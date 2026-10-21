"""Talking to the modern web: HTTPS-first fetching, redirects, decompression, header hygiene."""

import http.client
import socket
import ssl
import threading
import time
import zlib
from collections import OrderedDict
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

# Upstream servers always see a current desktop browser, never Python or the vintage client.
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
	'(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
ACCEPT_HTML = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/gif,image/jpeg,image/png,*/*;q=0.8'

# request headers never forwarded upstream (we set our own or they are hop-by-hop)
_DROP_REQUEST = {'connection', 'proxy-connection', 'keep-alive', 'proxy-authorization', 'te', 'trailer',
	'transfer-encoding', 'upgrade', 'host', 'accept-encoding', 'range', 'if-range', 'user-agent',
	'content-length', 'expect', 'upgrade-insecure-requests', 'extension', 'pragma', 'ua-cpu', 'ua-os',
	'ua-color', 'ua-pixels', 'ua-voice', 'x-forwarded-for', 'via'}


class UpstreamError(Exception):
	pass


def make_ssl_context(insecure=False):
	ctx = ssl.create_default_context()
	try:
		ctx.minimum_version = ssl.TLSVersion.TLSv1_2
	except (AttributeError, ValueError):
		pass
	if insecure:
		ctx.check_hostname = False
		ctx.verify_mode = ssl.CERT_NONE
	return ctx


def tls13_available():
	return getattr(ssl, 'HAS_TLSv1_3', False)


def ascii_url(url):
	"""IDNA-encode the host and percent-encode anything non-ASCII or unsafe."""
	parts = urlsplit(url)
	host = parts.hostname or ''
	try:
		host.encode('ascii')
	except UnicodeEncodeError:
		host = host.encode('idna').decode('ascii')
	netloc = host
	if parts.port:
		netloc += ':%d' % parts.port
	path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
	query = quote(parts.query, safe="/%:@!$&'()*+,;=-._~?[]|")
	return urlunsplit((parts.scheme, netloc, path, query, ''))


class Response:
	"""An upstream response with its body decoded from gzip/deflate on the fly."""

	def __init__(self, resp, conn, url):
		self.resp = resp
		self.conn = conn
		self.url = url
		self.status = resp.status
		self.reason = resp.reason
		self.headers = resp.getheaders()
		enc = (resp.getheader('content-encoding') or '').strip().lower()
		self._decoder = None
		self.encoded = False
		if enc in ('gzip', 'x-gzip'):
			self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
		elif enc == 'deflate':
			self._decoder = 'deflate'
		elif enc and enc != 'identity':
			self.encoded = True  # br/zstd we did not ask for: pass through untouched

	def header(self, name, default=None):
		name = name.lower()
		for k, v in self.headers:
			if k.lower() == name:
				return v
		return default

	def header_all(self, name):
		name = name.lower()
		return [v for k, v in self.headers if k.lower() == name]

	def _decode(self, data, final=False):
		if self._decoder is None:
			return data
		if self._decoder == 'deflate':  # zlib-wrapped or raw, servers disagree
			try:
				self._decoder = zlib.decompressobj()
				return self._decoder.decompress(data) + (self._decoder.flush() if final else b'')
			except zlib.error:
				self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
		try:
			out = self._decoder.decompress(data)
			if final:
				out += self._decoder.flush()
			return out
		except zlib.error:
			return b''

	def iter_body(self, chunk=65536):
		while True:
			try:
				data = self.resp.read(chunk)
			except (http.client.IncompleteRead, OSError) as e:
				data = getattr(e, 'partial', b'') or b''
				if data:
					yield self._decode(data)
				break
			if not data:
				break
			out = self._decode(data)
			if out:
				yield out
		tail = self._decode(b'', final=True) if self._decoder not in (None, 'deflate') else b''
		if tail:
			yield tail

	def read(self, limit):
		"""Read up to `limit` decoded bytes. Returns (data, rest_iterator_or_None)."""
		it = self.iter_body()
		buf = []
		size = 0
		for chunk in it:
			buf.append(chunk)
			size += len(chunk)
			if size > limit:
				return b''.join(buf), it
		return b''.join(buf), None

	def close(self):
		try:
			self.conn.close()
		except Exception:
			pass


class Upstream:
	def __init__(self, timeout=30, insecure=False, upgrade=True, user_agent=USER_AGENT, accept_language='en-US,en;q=0.8'):
		self.timeout = timeout
		self.ctx = make_ssl_context(insecure)
		self.upgrade = upgrade
		self.user_agent = user_agent
		self.accept_language = accept_language
		self.lock = threading.Lock()
		self.secure = OrderedDict()  # (host, client-visible port) -> True: served over https
		self.plain = {}              # (host, port) -> expiry: https failed, use http
		self.css_cache = OrderedDict()

	# ------------------------------------------------------------ scheme bookkeeping

	def note_secure(self, host, port):
		with self.lock:
			self.secure[(host.lower(), port)] = True
			self.secure.move_to_end((host.lower(), port))
			while len(self.secure) > 20000:
				self.secure.popitem(last=False)

	def is_secure(self, host, port):
		return (host.lower(), port) in self.secure

	def client_view(self, url):
		"""The http:// URL an old browser should use for an upstream URL (remembering https hosts)."""
		parts = urlsplit(url)
		if parts.scheme.lower() != 'https':
			return url
		host = parts.hostname or ''
		port = parts.port or 80  # https on 443 is presented as plain http on 80
		self.note_secure(host, port)
		netloc = parts.netloc.rsplit('@', 1)[-1]
		if parts.port in (None, 443):
			netloc = host if ':' not in host else '[%s]' % host
		return urlunsplit(('http', netloc, parts.path, parts.query, parts.fragment))

	def candidates(self, url):
		"""Upstream URLs to try, in order, for what the browser asked for."""
		parts = urlsplit(url)
		scheme = parts.scheme.lower()
		host = parts.hostname or ''
		if scheme == 'https':
			return [url]
		port = parts.port or 80
		rest = urlunsplit(('', '', parts.path or '/', parts.query, ''))
		hostport = host if ':' not in host else '[%s]' % host
		if self.is_secure(host, port):
			if port == 80:
				return ['https://%s%s' % (hostport, rest)]
			return ['https://%s:%d%s' % (hostport, port, rest)]
		if port != 80 or not self.upgrade:
			return [url]
		expiry = self.plain.get((host.lower(), 80))
		if expiry and expiry > time.time():
			return [url]
		return ['https://%s%s' % (hostport, rest), url]

	# ------------------------------------------------------------ requests

	def build_headers(self, client_headers, url):
		headers = []
		accept = None
		for k, v in client_headers:
			lk = k.lower()
			if lk in _DROP_REQUEST:
				continue
			if lk == 'accept':
				accept = v
				continue
			if lk == 'referer':
				v = self.upstream_referer(v)
			headers.append((k, v))
		if not accept or accept.strip().startswith('*/*') or 'text/html' not in accept and 'text/css' not in accept:
			accept = ACCEPT_HTML  # old browsers send image-only lists like "image/gif, image/jpeg, */*"
		headers.append(('Accept', accept))
		if not any(k.lower() == 'accept-language' for k, _ in headers):
			headers.append(('Accept-Language', self.accept_language))
		headers.append(('User-Agent', self.user_agent))
		return headers

	def upstream_referer(self, ref):
		parts = urlsplit(ref)
		if parts.scheme == 'http' and self.is_secure(parts.hostname or '', parts.port or 80):
			return urlunsplit(('https', parts.hostname, parts.path, parts.query, ''))
		return ref

	def _open(self, method, url, headers, body, probe):
		url = ascii_url(url)
		parts = urlsplit(url)
		host = parts.hostname
		if not host:
			raise UpstreamError('No host in URL')
		path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
		if parts.scheme == 'https':
			conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=6 if probe else self.timeout, context=self.ctx)
		else:
			conn = http.client.HTTPConnection(host, parts.port or 80, timeout=self.timeout)
		conn.connect()
		if probe:
			conn.sock.settimeout(self.timeout)
		conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
		hosthdr = host if ':' not in host else '[%s]' % host
		if parts.port:
			hosthdr += ':%d' % parts.port
		conn.putheader('Host', hosthdr)
		for k, v in headers:
			conn.putheader(k, v)
		conn.putheader('Accept-Encoding', 'gzip, deflate')
		conn.putheader('Connection', 'close')
		if body is not None or method in ('POST', 'PUT', 'PATCH'):
			conn.putheader('Content-Length', str(len(body or b'')))
		conn.endheaders(body)
		return Response(conn.getresponse(), conn, url)

	def request(self, method, url, client_headers, body=None, max_redirects=6):
		"""Fetch `url` (as the browser sees it). Redirects that the browser would not notice (http->https
		of the same address) are followed here; all others are returned for the browser to follow."""
		headers = self.build_headers(client_headers, url)
		cands = self.candidates(url)
		resp = None
		last_err = None
		for i, cand in enumerate(cands):
			fallback_left = i + 1 < len(cands)
			try:
				resp = self._open(method, cand, headers, body, probe=fallback_left)
				break
			except socket.gaierror as e:
				raise UpstreamError('Cannot resolve host: %s' % e)
			except (OSError, ssl.SSLError, http.client.HTTPException) as e:
				last_err = e
				if fallback_left:
					p = urlsplit(url)
					self.plain[((p.hostname or '').lower(), p.port or 80)] = time.time() + 3600
					continue
				if isinstance(e, ssl.SSLError) and 'PROTOCOL_VERSION' in str(e) and not tls13_available():
					raise UpstreamError('%s requires TLS 1.3, which this Python (%s) cannot speak. '
						'Run the proxy with a Python linked against OpenSSL 3.' % (urlsplit(cand).hostname, ssl.OPENSSL_VERSION))
				raise UpstreamError('%s: %s' % (type(e).__name__, e))
		if resp is None:
			raise UpstreamError(str(last_err))
		if resp.url.startswith('https:'):
			self.client_view(resp.url)

		for _ in range(max_redirects):
			if resp.status not in (301, 302, 303, 307, 308):
				break
			loc = resp.header('location')
			if not loc:
				break
			target = urljoin(resp.url, loc.strip())
			if self.client_view(target).split('#')[0] != self.client_view(resp.url).split('#')[0]:
				break  # a real redirect: let the browser see it
			resp.close()
			if resp.status == 303 or resp.status in (301, 302) and method == 'POST':
				method, body = 'GET', None
			try:
				resp = self._open(method, target, headers, body, probe=False)
			except (OSError, ssl.SSLError, http.client.HTTPException) as e:
				raise UpstreamError('%s: %s' % (type(e).__name__, e))
		return resp

	def fetch_text(self, url, limit=1500000):
		"""GET a stylesheet; cached briefly. Returns text or None."""
		from .text import decode_body
		now = time.time()
		with self.lock:
			hit = self.css_cache.get(url)
			if hit and hit[0] > now:
				return hit[1]
		text = None
		try:
			resp = self.request('GET', self.client_view(url), [('Accept', 'text/css,*/*;q=0.1')])
			try:
				if resp.status == 200 and not resp.encoded:
					data, rest = resp.read(limit)
					if rest is None:
						text = decode_body(data, resp.header('content-type'), sniff_html=False)
			finally:
				resp.close()
		except Exception:
			text = None
		with self.lock:
			self.css_cache[url] = (now + 600, text)
			while len(self.css_cache) > 64:
				self.css_cache.popitem(last=False)
		return text
