"""Listening sockets and the three ways in: HTTP proxy, SOCKS 4/4a/5, and URL-prefix gateway.

Every listener speaks every protocol; the first byte of a connection tells them apart."""

import email.utils
import ipaddress
import re
import select
import socket
import socketserver
import struct
import sys
import threading
import time
from http import HTTPStatus
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from . import browsers, images, pages
from .html_rewrite import rewrite_html
from .text import decode_body, latin1_query_to_utf8, to_latin1
from .upstream import Upstream, UpstreamError, ascii_url

HTTP_PORTS = {80, 8000, 8008, 8080, 8888}
HTML_TYPES = ('text/html', 'application/xhtml+xml')
MAX_HTML = 8 * 1024 * 1024
MAX_IMAGE = 24 * 1024 * 1024
_METHOD = re.compile(rb'^[A-Z]{3,10} \S')
_FILELIKE = re.compile(r'\.(ico|png|gif|jpe?g|html?|txt|css|js|xml|json|pac|dat)$', re.I)

DEFAULT_ALLOW = ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16',
	'100.64.0.0/10', '::1/128', 'fc00::/7', 'fe80::/10']


class Settings:
	def __init__(self, **kw):
		self.listen = [('', 8080)]
		self.verbose = False
		self.image_max_size = 480
		self.image_max_kb = 8
		self.images = True
		self.css = True
		self.default_profile = 'netscape3'
		self.force_profile = None
		self.viewport = 800
		self.allow = DEFAULT_ALLOW
		self.socks_intercept = True
		self.tls_description = ''
		self.__dict__.update(kw)
		self.allow_nets = [ipaddress.ip_network(n, strict=False) for n in self.allow]


class ClientError(Exception):
	pass


class Conn:
	"""A client socket with a read buffer, so protocols can be sniffed and requests parsed."""

	def __init__(self, sock):
		self.sock = sock
		self.buf = b''

	def fill(self):
		data = self.sock.recv(65536)
		if not data:
			raise EOFError
		self.buf += data

	def peek(self, n):
		while len(self.buf) < n:
			self.fill()
		return self.buf[:n]

	def read_exact(self, n):
		data = self.peek(n)
		self.buf = self.buf[n:]
		return data

	def read_until(self, delim, limit=65536):
		while True:
			i = self.buf.find(delim)
			if i >= 0:
				data = self.buf[:i]
				self.buf = self.buf[i + len(delim):]
				return data
			if len(self.buf) > limit:
				raise ClientError('request too large')
			self.fill()

	def take(self):
		data, self.buf = self.buf, b''
		return data

	def send(self, data):
		self.sock.sendall(data)


class Request:
	def __init__(self):
		self.method = 'GET'
		self.target = '/'
		self.version = 'HTTP/1.0'
		self.headers = []
		self.body = None

	def header(self, name, default=None):
		name = name.lower()
		for k, v in self.headers:
			if k.lower() == name:
				return v
		return default


def read_request(conn):
	line = conn.read_until(b'\n').rstrip(b'\r')
	while not line.strip():  # tolerate stray blank lines between requests
		line = conn.read_until(b'\n').rstrip(b'\r')
	req = Request()
	parts = line.decode('latin-1').split()
	if len(parts) == 2:  # HTTP/0.9: "GET /path", no headers
		req.method, req.target = parts
		req.version = 'HTTP/0.9'
		return req
	if len(parts) != 3:
		raise ClientError('bad request line: %r' % line[:100])
	req.method, req.target, req.version = parts[0].upper(), parts[1], parts[2].upper()
	while True:
		h = conn.read_until(b'\n').rstrip(b'\r').decode('latin-1')
		if not h:
			break
		if h[0] in ' \t' and req.headers:
			k, v = req.headers[-1]
			req.headers[-1] = (k, v + ' ' + h.strip())
			continue
		if ':' in h:
			k, v = h.split(':', 1)
			req.headers.append((k.strip(), v.strip()))
	length = req.header('content-length')
	if length:
		try:
			n = int(length)
		except ValueError:
			raise ClientError('bad content-length')
		if n > 64 * 1024 * 1024:
			raise ClientError('request body too large')
		req.body = conn.read_exact(n) if n else b''
	elif req.header('transfer-encoding', '').lower() == 'chunked':
		chunks = []
		while True:
			size = int(conn.read_until(b'\r\n').split(b';')[0].strip() or b'0', 16)
			if size == 0:
				conn.read_until(b'\r\n')
				break
			chunks.append(conn.read_exact(size))
			conn.read_until(b'\r\n')
		req.body = b''.join(chunks)
	return req


# ---------------------------------------------------------------- responses

def _http_date(t=None):
	return email.utils.formatdate(t, usegmt=True)


def _cookie_date(t):
	# Netscape's original cookie spec format, which every old browser parses
	return time.strftime('%a, %d-%b-%Y %H:%M:%S GMT', time.gmtime(t))


def rewrite_cookie(value, gateway_path=None):
	parts = [p.strip() for p in value.split(';')]
	out = [parts[0]]
	has_expires = False
	max_age = None
	for p in parts[1:]:
		name = p.split('=', 1)[0].strip().lower()
		if name in ('secure', 'samesite', 'partitioned', 'httponly', 'priority'):
			continue
		if name == 'max-age':
			try:
				max_age = int(p.split('=', 1)[1])
			except (ValueError, IndexError):
				pass
			continue
		if name == 'expires':
			try:
				t = email.utils.parsedate_to_datetime(p.split('=', 1)[1].strip()).timestamp()
				p = 'expires=' + _cookie_date(t)
				has_expires = True
			except (TypeError, ValueError, IndexError, OverflowError):
				continue
		if gateway_path is not None and name in ('domain', 'path'):
			continue
		out.append(p)
	if max_age is not None and not has_expires:
		out.append('expires=' + _cookie_date(time.time() + max_age))
	if gateway_path is not None:
		out.append('path=' + gateway_path)
	return '; '.join(out)


class Exchange:
	"""One request from an old browser and how to answer it."""

	def __init__(self, server, conn, req, mode, base=None):
		self.server = server
		self.settings = server.settings
		self.up = server.upstream
		self.conn = conn
		self.req = req
		self.mode = mode           # 'proxy', 'socks' or 'gateway'
		self.base = base           # http://proxyhost:port, for gateway URLs and our own pages
		ua = req.header('user-agent')
		self.profile = browsers.PROFILES.get(self.settings.force_profile) or browsers.detect(ua, self.settings.default_profile)
		self.started = time.time()
		self.status = 0
		self.sent = 0
		self.url = ''

	# ------------------------------------------------------------ url mapping

	def map_url(self, upstream_url):
		view = self.up.client_view(upstream_url)
		if self.mode == 'gateway':
			return '%s/%s' % (self.base, view)
		return view

	def gateway_cookie_path(self):
		if self.mode != 'gateway':
			return None
		p = urlsplit(self.url)
		return '/%s://%s' % (p.scheme, p.netloc)

	# ------------------------------------------------------------ output

	def send(self, status, headers, body=None, stream=None):
		self.status = status
		if self.req.version == 'HTTP/0.9':
			if body:
				self.conn.send(body)
			return self._send_stream(stream)
		try:
			reason = HTTPStatus(status).phrase
		except ValueError:
			reason = 'Unknown'
		lines = ['HTTP/1.0 %d %s' % (status, reason)]
		names = {k.lower() for k, _ in headers}
		for k, v in headers:
			lines.append('%s: %s' % (k, v))
		if 'date' not in names:
			lines.append('Date: ' + _http_date())
		lines.append('Server: LegacyProxy')
		lines.append('Connection: close')
		if body is not None and stream is None:
			lines.append('Content-Length: %d' % len(body))
		head = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1', 'replace')
		self.conn.send(head + (body or b'') if len(body or b'') < 65536 else head)
		if body and len(body) >= 65536:
			self.conn.send(body)
		self.sent += len(body or b'')
		self._send_stream(stream)

	def _send_stream(self, stream):
		if stream is None:
			return
		for chunk in stream:
			self.conn.send(chunk)
			self.sent += len(chunk)

	def send_html(self, status, html, extra=()):
		self.send(status, [('Content-Type', 'text/html')] + list(extra), html.encode('latin-1', 'replace'))

	def send_error(self, status, title, message):
		self.send_html(status, pages.error_page(title, message, self.url or None))

	def log(self):
		ms = int((time.time() - self.started) * 1000)
		sys.stdout.write('%s %-7s %-12s %s %s %d %dB %dms\n' % (time.strftime('%H:%M:%S'), self.mode,
			self.profile.key, self.req.method, self.url or self.req.target, self.status, self.sent, ms))
		sys.stdout.flush()

	# ------------------------------------------------------------ handling

	def run(self, url):
		self.url = url
		try:
			self._proxy(url)
		except UpstreamError as e:
			self.send_error(502, 'Could not load page', str(e))
		finally:
			self.log()

	def own_page(self, path):
		"""Requests addressed to the proxy itself: home page, the Go box, or a /http://... gateway URL."""
		if path.startswith('/go?') or path == '/go':
			q = parse_qs(urlsplit(path).query)
			target = (q.get('url') or [''])[0].strip()
			if not target or target == 'http://':
				return self.redirect(self.base + '/')
			if '://' not in target:
				target = 'http://' + target
			target = self.map_url(target) if self.mode == 'gateway' else self.up.client_view(target)
			return self.redirect(target)
		if path.split('?')[0].lower() in ('/proxy.pac', '/wpad.dat', '/pac'):
			hostport = self.base.split('//', 1)[1]
			self.send(200, [('Content-Type', 'application/x-ns-proxy-autoconfig')],
				pages.pac_file(hostport).encode('ascii'))
			return self.log()
		target = gateway_target(path)
		if target:
			self.mode = 'gateway'
			return self.run(target)
		if path in ('/', '/index.html', '/legacyproxy'):
			self.status = 200
			self.send_html(200, pages.home_page(self.profile, self.settings, self.base, self.server.image_backend,
				self.mode))
		else:
			self.send_error(404, 'Not found', 'The proxy has no page called %s.' % path)
		self.log()

	def redirect(self, location):
		self.send(302, [('Location', location), ('Content-Type', 'text/html')],
			('<a href="%s">Continue</a>' % location).encode('latin-1', 'replace'))
		self.log()

	def _proxy(self, url):
		req = self.req
		body = req.body
		ctype_req = (req.header('content-type') or '').lower()
		if body and ctype_req.startswith('application/x-www-form-urlencoded'):
			body = latin1_query_to_utf8(body.decode('latin-1')).encode('ascii', 'replace')
		parts = urlsplit(url)
		if parts.query:
			url = url.split('?', 1)[0] + '?' + latin1_query_to_utf8(parts.query)
		resp = self.up.request(req.method, url, req.headers, body)
		try:
			self._respond(resp)
		finally:
			resp.close()

	def _respond(self, resp):
		status = resp.status
		ctype = (resp.header('content-type') or '').strip()
		ctype_l = ctype.lower().split(';')[0].strip()
		out_headers = self._response_headers(resp, ctype_l)
		no_body = self.req.method == 'HEAD' or status in (204, 304) or 100 <= status < 200

		if status in (303, 307):
			status = 302
		elif status == 308:
			status = 301
		if no_body:
			return self.send(status, out_headers + [('Content-Type', ctype)] if ctype else out_headers, b'')

		if resp.encoded:
			out_headers += [('Content-Type', ctype), ('Content-Encoding', resp.header('content-encoding'))]
			return self.send(status, out_headers, stream=resp.iter_body())

		if ctype_l in HTML_TYPES or (not ctype_l and status >= 300):
			data, rest = resp.read(MAX_HTML)
			if rest is None:
				return self._send_page(status, out_headers, data, ctype, resp.url)
			return self.send(status, out_headers + [('Content-Type', 'text/html')], data, stream=rest)

		if ctype_l.startswith('image/') and self.settings.images:
			data, rest = resp.read(MAX_IMAGE)
			if rest is not None:
				return self.send(status, out_headers + [('Content-Type', ctype)], data, stream=rest)
			if status != 200:
				return self.send(status, out_headers + [('Content-Type', ctype)], data)
			result = images.shrink(data, self.server.image_backend, self.profile,
				self.settings.image_max_size, self.settings.image_max_kb * 1024)
			if result is None:
				return self.send(415, [('Content-Type', 'text/plain')], b'Image could not be converted.\n')
			return self.send(status, out_headers + [('Content-Type', result[1])], result[0])

		if ctype_l == 'text/plain':
			data, rest = resp.read(MAX_HTML)
			if rest is None:
				text = decode_body(data, ctype, sniff_html=False)
				return self.send(status, out_headers + [('Content-Type', 'text/plain')], to_latin1(text))
			return self.send(status, out_headers + [('Content-Type', ctype)], data, stream=rest)

		if ctype:
			out_headers.append(('Content-Type', ctype))
		length = resp.header('content-length')
		if length and resp._decoder is None:
			out_headers.append(('Content-Length', length))
		self.send(status, out_headers, stream=resp.iter_body())

	def _send_page(self, status, headers, data, ctype, upstream_url):
		text = decode_body(data, ctype)
		fetch_css = self.up.fetch_text if self.settings.css else None
		try:
			html = rewrite_html(text, upstream_url, self.profile, self.map_url, fetch_css,
				self.settings.image_max_size, self.settings.viewport)
		except RecursionError:
			html = pages.error_page('Page too complex', 'This page is nested too deeply to convert.', self.url)
		self.send(status, headers + [('Content-Type', 'text/html')], html.encode('latin-1', 'replace'))

	def _response_headers(self, resp, ctype_l):
		out = []
		cookie_path = self.gateway_cookie_path()
		cache = (resp.header('cache-control') or '').lower()
		for k, v in resp.headers:
			lk = k.lower()
			if lk == 'set-cookie':
				out.append(('Set-Cookie', rewrite_cookie(v, cookie_path)))
			elif lk == 'location':
				out.append(('Location', self.map_url(ascii_url(urljoin(resp.url, v.strip())))))
			elif lk == 'refresh':
				m = re.match(r'\s*(\d+)\s*[;,]\s*url\s*=\s*(.*)$', v, re.I)
				if m:
					out.append(('Refresh', '%s; URL=%s' % (m.group(1), self.map_url(urljoin(resp.url, m.group(2).strip(' "\''))))))
			elif lk in ('last-modified', 'expires', 'content-disposition', 'www-authenticate', 'content-language'):
				out.append((k, v))
		if 'no-store' in cache or 'no-cache' in cache or 'private' in cache:
			out.append(('Pragma', 'no-cache'))
		elif not resp.header('expires') and ctype_l not in HTML_TYPES:
			m = re.search(r'max-age\s*=\s*(\d+)', cache)
			if m:
				out.append(('Expires', _http_date(time.time() + int(m.group(1)))))
		return out


def gateway_target(path):
	"""'/http://host/x', '/http:/host/x' (slashes merged), '/host.tld/x' or WAIS-style '/host:210/x' -> URL."""
	p = path[1:]
	m = re.match(r'^(https?):/+(.*)$', p, re.I)
	if m:
		return '%s://%s' % (m.group(1).lower(), m.group(2))
	host = p.split('/', 1)[0].split('?', 1)[0]
	hostname = host.split(':', 1)[0]
	if '.' in hostname and not _FILELIKE.search(hostname) and re.match(r'^[\w.-]+(:\d+)?$', host):
		rest = p[len(host):] or '/'
		if host.endswith(':210'):  # WAIS port: the user meant the web site
			host = host[:-4]
		if rest.startswith('?'):
			rest = '/' + rest
		return 'http://%s%s' % (host, unquote(rest) if rest.startswith('/%') else rest)
	return None


# ---------------------------------------------------------------- connection handling

def relay(a, b, initial=b'', idle=300):
	"""Copy bytes both ways until either side closes."""
	if initial:
		b.sendall(initial)
	socks = [a, b]
	try:
		while True:
			r, _, _ = select.select(socks, [], [], idle)
			if not r:
				return
			for s in r:
				data = s.recv(65536)
				if not data:
					return
				(b if s is a else a).sendall(data)
	except OSError:
		return
	finally:
		b.close()


class Handler(socketserver.BaseRequestHandler):
	def handle(self):
		srv = self.server.app
		sock = self.request
		addr = self.client_address[0]
		try:
			ip = ipaddress.ip_address(addr.split('%')[0])
		except ValueError:
			return
		if not any(ip in n for n in srv.settings.allow_nets):
			sys.stdout.write('refused connection from %s (not in --allow)\n' % addr)
			return
		sock.settimeout(120)
		conn = Conn(sock)
		try:
			first = conn.peek(1)
			if first == b'\x04':
				self.socks4(srv, conn)
			elif first == b'\x05':
				self.socks5(srv, conn)
			else:
				self.http(srv, conn)
		except (EOFError, ConnectionError, socket.timeout):
			pass
		except ClientError as e:
			if srv.settings.verbose:
				sys.stdout.write('client error from %s: %s\n' % (addr, e))
		except Exception as e:  # never let one page take the proxy down
			sys.stdout.write('error handling %s: %s: %s\n' % (addr, type(e).__name__, e))
			if srv.settings.verbose:
				import traceback
				traceback.print_exc()

	def base_url(self, req):
		host = req.header('host')
		local_ip, local_port = self.request.getsockname()[:2]
		if not host:
			host = local_ip if ':' not in local_ip else '[%s]' % local_ip
			host += ':%d' % local_port
		return 'http://' + host

	def is_self(self, srv, host, port):
		local_ip, local_port = self.request.getsockname()[:2]
		if port != local_port:
			return False
		host = (host or '').lower()
		return host in srv.self_names or host == local_ip

	def http(self, srv, conn, socks_target=None):
		req = read_request(conn)
		if srv.settings.verbose:
			sys.stdout.write('> %s %s %s\n' % (req.method, req.target, req.headers))
		if req.method == 'CONNECT':
			return self.connect(srv, conn, req)
		target = req.target
		if socks_target:
			host_hdr = req.header('host')
			host, port = socks_target
			if target.startswith('/'):
				if host_hdr:
					host = re.sub(r':\d+$', '', host_hdr.strip())
				url = 'http://%s%s%s' % (host, '' if port == 80 else ':%d' % port, target)
			else:
				url = target
			return Exchange(srv, conn, req, 'socks').run(url)
		if target.startswith('/'):
			return Exchange(srv, conn, req, 'gateway', self.base_url(req)).own_page(target)
		if '://' not in target:
			target = 'http://' + target
		p = urlsplit(target)
		scheme = p.scheme.lower()
		if scheme not in ('http', 'https'):
			ex = Exchange(srv, conn, req, 'proxy')
			ex.url = target
			ex.send_error(501, 'Not supported', 'This proxy only fetches web pages (http and https).')
			return ex.log()
		if self.is_self(srv, p.hostname, p.port or 80) or (p.hostname or '').lower() == 'legacyproxy':
			base = 'http://%s' % p.netloc
			ex = Exchange(srv, conn, req, 'proxy', base)
			return ex.own_page(urlsplit(target)._replace(scheme='', netloc='').geturl() or '/')
		Exchange(srv, conn, req, 'proxy', self.base_url(req)).run(target)

	def connect(self, srv, conn, req):
		host, _, port = req.target.rpartition(':')
		host = host.strip('[]')
		try:
			upstream = socket.create_connection((host, int(port or 443)), timeout=15)
		except (OSError, ValueError) as e:
			conn.send(b'HTTP/1.0 502 Bad Gateway\r\n\r\n')
			sys.stdout.write('CONNECT %s failed: %s\n' % (req.target, e))
			return
		conn.send(b'HTTP/1.0 200 Connection established\r\n\r\n')
		if srv.settings.verbose:
			sys.stdout.write('CONNECT %s (tunnelled untouched)\n' % req.target)
		upstream.settimeout(None)
		self.request.settimeout(None)
		relay(self.request, upstream, conn.take())

	# ------------------------------------------------------------ SOCKS

	def socks4(self, srv, conn):
		_, cmd, port = struct.unpack('>BBH', conn.read_exact(4))
		ip = conn.read_exact(4)
		conn.read_until(b'\x00', 1024)  # user id
		if ip[:3] == b'\x00\x00\x00' and ip[3] != 0:
			host = conn.read_until(b'\x00', 1024).decode('ascii', 'replace')  # SOCKS4a
		else:
			host = socket.inet_ntoa(ip)
		ok = b'\x00\x5a' + struct.pack('>H', port) + ip
		fail = b'\x00\x5b' + struct.pack('>H', port) + ip
		if cmd != 1:
			return conn.send(fail)
		self.socks_connected(srv, conn, host, port, ok, fail)

	def socks5(self, srv, conn):
		_, n = struct.unpack('>BB', conn.read_exact(2))
		methods = conn.read_exact(n)
		if 0 not in methods:
			return conn.send(b'\x05\xff')
		conn.send(b'\x05\x00')
		_, cmd, _, atyp = struct.unpack('>BBBB', conn.read_exact(4))
		if atyp == 1:
			host = socket.inet_ntoa(conn.read_exact(4))
		elif atyp == 3:
			host = conn.read_exact(ord(conn.read_exact(1))).decode('ascii', 'replace')
		elif atyp == 4:
			host = socket.inet_ntop(socket.AF_INET6, conn.read_exact(16))
		else:
			return conn.send(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
		port = struct.unpack('>H', conn.read_exact(2))[0]
		ok = b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00'
		fail = b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00'
		if cmd != 1:
			return conn.send(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
		self.socks_connected(srv, conn, host, port, ok, fail)

	def socks_connected(self, srv, conn, host, port, ok, fail):
		if port in HTTP_PORTS and srv.settings.socks_intercept:
			conn.send(ok)
			try:
				conn.peek(8)
			except EOFError:
				return
			if _METHOD.match(conn.buf):
				return self.http(srv, conn, socks_target=(host, port))
			upstream = self._dial(host, port)
			if upstream:
				relay(self.request, upstream, conn.take())
			return
		upstream = self._dial(host, port)
		if not upstream:
			return conn.send(fail)
		conn.send(ok)
		if srv.settings.verbose:
			sys.stdout.write('SOCKS %s:%d (tunnelled untouched)\n' % (host, port))
		relay(self.request, upstream, conn.take())

	def _dial(self, host, port):
		try:
			s = socket.create_connection((host, port), timeout=15)
			s.settimeout(None)
			self.request.settimeout(None)
			return s
		except OSError:
			return None


class ThreadingServer(socketserver.ThreadingTCPServer):
	daemon_threads = True
	allow_reuse_address = True
	request_queue_size = 64


class Proxy:
	def __init__(self, settings):
		self.settings = settings
		self.upstream = Upstream(timeout=30, insecure=settings.insecure, upgrade=settings.upgrade,
			user_agent=settings.user_agent)
		self.image_backend = images.find_backends(settings.image_backend) if settings.images else None
		self.self_names = {'localhost', '127.0.0.1', '::1', 'legacyproxy', socket.gethostname().lower()}
		try:
			self.self_names.add(socket.gethostbyname(socket.gethostname()))
		except OSError:
			pass
		self.servers = []

	def serve_forever(self):
		threading.stack_size(16 * 1024 * 1024)  # deep pages recurse; give worker threads room
		sys.setrecursionlimit(5000)
		for host, port in self.settings.listen:
			srv = ThreadingServer((host, port), Handler)
			srv.app = self
			self.servers.append(srv)
		threads = []
		for srv in self.servers:
			t = threading.Thread(target=srv.serve_forever, daemon=True)
			t.start()
			threads.append(t)
		try:
			while True:
				time.sleep(3600)
		except KeyboardInterrupt:
			pass
		finally:
			for srv in self.servers:
				srv.shutdown()
