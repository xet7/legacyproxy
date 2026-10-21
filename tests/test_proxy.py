"""Run with: python3 -m unittest discover tests"""

import http.server
import socket
import struct
import threading
import unittest

from legacyproxy import browsers, css, images
from legacyproxy.html_rewrite import rewrite_html
from legacyproxy.server import Proxy, Settings, gateway_target, rewrite_cookie
from legacyproxy.text import latin1_query_to_utf8, to_legacy_html

P = browsers.PROFILES


def plain(u):
	return u.replace('https://', 'http://')


class BrowserTests(unittest.TestCase):
	def test_detect(self):
		cases = {
			'NCSA Mosaic/2.0.0b4 (Macintosh)': 'mosaic',
			'Mozilla/1.0N (Macintosh)': 'netscape1',
			'Mozilla/1.1N (Macintosh; I; 68K)': 'netscape1.1',
			'Mozilla/3.01 (Macintosh; I; PPC)': 'netscape3',
			'Mozilla/3.01Gold (Macintosh; I; PPC)': 'netscape3',
			'Mozilla/4.08 [en] (Macintosh; I; PPC)': 'netscape4',
			'Mozilla/2.0 (compatible; MSIE 3.01; Mac_PowerPC)': 'ie3',
			'Mozilla/4.0 (compatible; MSIE 4.01; Mac_PowerPC)': 'ie4',
			'Mozilla/4.0 (compatible; MSIE 5.0; Mac_PowerPC)': 'ie5',
			'OmniWeb/3.0.2 OWF/1.0': 'omniweb',
			'Mozilla/4.5 (compatible; OmniWeb/4.1-v422; Mac_PowerPC)': 'omniweb4',
		}
		for ua, key in cases.items():
			self.assertEqual(browsers.detect(ua).key, key, ua)


class CssTests(unittest.TestCase):
	def test_colors(self):
		self.assertEqual(css.parse_color('#abc'), '#aabbcc')
		self.assertEqual(css.parse_color('rgb(255 0 0 / 80%)'), '#ff0000')
		self.assertEqual(css.parse_color('rgba(0,0,0,0.1)'), None)
		self.assertEqual(css.parse_color('hsl(120, 100%, 25%)'), '#008000')
		self.assertEqual(css.parse_color('oklch(1 0 0)'), '#ffffff')
		self.assertEqual(css.parse_color('transparent'), None)

	def test_media(self):
		self.assertTrue(css.media_matches('screen and (min-width: 640px)'))
		self.assertFalse(css.media_matches('(min-width: 1200px)'))
		self.assertFalse(css.media_matches('print'))
		self.assertTrue(css.media_matches('(width >= 40em)'))
		self.assertFalse(css.media_matches('(prefers-color-scheme: dark)'))

	def test_nesting_and_vars(self):
		sheet = css.parse_stylesheet(':root{--c:#f00} .a{color:var(--c); .b{color:blue}} @supports (display:grid){.x{color:red}}')
		self.assertEqual(sheet.vars['--c'], '#f00')
		self.assertEqual([r.selector_text for r in sheet.rules], [['.a']])


class RewriteTests(unittest.TestCase):
	PAGE = '''<html><head><title>T’s</title><style>
		.hide{display:none} .sr{position:absolute;width:1px;height:1px;clip:rect(0 0 0 0)}
		.c{color:#c00;text-align:center} body{background:#000;color:#eee}</style></head>
		<body><nav class=hide>menu</nav><span class=sr>skip</span><section><p class=c>Red</p></section>
		<a href="https://x.test/p?a=1&b=2">link</a><a href="javascript:go()">js</a>
		<img src="data:," data-src="/big.jpg" width=2000 height=1000 alt=pic>
		<table><tr><td>a<td>b</table><form action=/s><input type=search name=q><button>Go</button></form>
		<button type=button>menu</button><script>x()</script></body></html>'''

	def render(self, key):
		return rewrite_html(self.PAGE, 'https://x.test/', P[key], plain)

	def test_netscape3(self):
		out = self.render('netscape3')
		self.assertNotIn('menu', out)
		self.assertNotIn('skip', out)
		self.assertNotIn('<script', out)
		self.assertIn('bgcolor="#000000"', out)
		self.assertIn('<p align="center"><font color="#cc0000">Red</font></p>', out)
		self.assertIn('href="http://x.test/p?a=1&amp;b=2"', out)
		self.assertIn('js', out)
		self.assertNotIn('javascript', out)
		self.assertIn('src="http://x.test/big.jpg" alt="pic" width="480" height="240"', out)
		self.assertIn('<input type="submit" value="Go">', out)
		self.assertIn('<title>T\'s</title>', out)
		self.assertIn('action="http://x.test/s"', out)

	def test_mosaic(self):
		out = self.render('mosaic')
		self.assertNotIn('<table', out)
		self.assertNotIn('<div', out)
		self.assertNotIn('<font', out)
		self.assertNotIn('bgcolor', out)

	def test_ie5_gets_css(self):
		out = self.render('ie5')
		self.assertIn('<style type="text/css">', out)
		self.assertIn('.c { color: #cc0000; text-align: center }', out)


class TextTests(unittest.TestCase):
	def test_entities(self):
		self.assertEqual(to_legacy_html('café — ½'), 'caf&eacute; -- &frac12;')
		self.assertEqual(to_legacy_html('½  x', P['netscape1']), '1/2  x')

	def test_form_transcode(self):
		self.assertEqual(latin1_query_to_utf8('q=caf%E9'), 'q=caf%C3%A9')
		self.assertEqual(latin1_query_to_utf8('q=caf%C3%A9'), 'q=caf%C3%A9')

	def test_cookie(self):
		c = rewrite_cookie('id=1; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=60')
		self.assertTrue(c.startswith('id=1; Path=/; expires='))
		self.assertNotIn('Secure', c)

	def test_gateway_paths(self):
		self.assertEqual(gateway_target('/http://a.com/x?y'), 'http://a.com/x?y')
		self.assertEqual(gateway_target('/http:/a.com/x'), 'http://a.com/x')
		self.assertEqual(gateway_target('/www.a.com:210/'), 'http://www.a.com/')
		self.assertIsNone(gateway_target('/favicon.ico'))
		self.assertIsNone(gateway_target('/proxy.pac'))


class ImageTests(unittest.TestCase):
	def test_shrink(self):
		backends = images.find_backends()
		if not backends:
			self.skipTest('no image converter installed')
		gif = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00'
			b',\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
		self.assertEqual(images.shrink(gif, backends, P['netscape3'])[1], 'image/gif')
		svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="300"><rect width="900" height="300" fill="red"/></svg>'
		result = images.shrink(svg, backends, P['netscape3'])
		if result:  # Pillow alone cannot read SVG
			info = images.image_info(result[0])
			self.assertLessEqual(max(info[1], info[2]), 480)


class Origin(http.server.BaseHTTPRequestHandler):
	def do_GET(self):
		body = ('<html><body><p>Hello from %s</p><a href="/next">n</a></body></html>' % self.path).encode()
		self.send_response(200)
		self.send_header('Content-Type', 'text/html; charset=utf-8')
		self.send_header('Set-Cookie', 'a=b; Secure')
		self.send_header('Content-Length', str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, *a):
		pass


class EndToEnd(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
		threading.Thread(target=cls.origin.serve_forever, daemon=True).start()
		cls.oport = cls.origin.server_address[1]
		import sys, io
		cls._stdout, sys.stdout = sys.stdout, io.StringIO()  # keep the access log out of test output
		settings = Settings(listen=[('127.0.0.1', 0)], images=False, css=False, insecure=False, upgrade=False,
			user_agent='test', image_backend=None)
		cls.proxy = Proxy(settings)
		threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
		import time
		while not cls.proxy.servers:
			time.sleep(0.01)
		cls.pport = cls.proxy.servers[0].server_address[1]

	@classmethod
	def tearDownClass(cls):
		import sys
		sys.stdout = cls._stdout
		cls.origin.shutdown()

	def talk(self, first, request):
		s = socket.create_connection(('127.0.0.1', self.pport))
		if first:
			s.sendall(first)
			s.recv(10)
		s.sendall(request)
		data = b''
		while True:
			chunk = s.recv(65536)
			if not chunk:
				break
			data += chunk
		s.close()
		return data.decode('latin-1')

	def test_http_proxy(self):
		out = self.talk(None, b'GET http://127.0.0.1:%d/a HTTP/1.0\r\nUser-Agent: Mozilla/3.01 (Macintosh)\r\n\r\n' % self.oport)
		self.assertTrue(out.startswith('HTTP/1.0 200'))
		self.assertIn('Hello from /a', out)
		self.assertIn('href="http://127.0.0.1:%d/next"' % self.oport, out)
		self.assertIn('Set-Cookie: a=b\r\n', out)

	def test_socks4(self):
		hello = struct.pack('>BBH', 4, 1, self.oport) + socket.inet_aton('127.0.0.1') + b'\x00'
		out = self.talk(hello, b'GET /s4 HTTP/1.0\r\n\r\n')
		self.assertIn('Hello from /s4', out)

	def test_socks5(self):
		s = socket.create_connection(('127.0.0.1', self.pport))
		s.sendall(b'\x05\x01\x00')
		self.assertEqual(s.recv(2), b'\x05\x00')
		s.sendall(b'\x05\x01\x00\x03\x09127.0.0.1' + struct.pack('>H', self.oport))
		self.assertEqual(s.recv(10)[:2], b'\x05\x00')
		s.sendall(b'GET /s5 HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
		data = b''
		while True:
			c = s.recv(65536)
			if not c:
				break
			data += c
		s.close()
		self.assertIn(b'Hello from /s5', data)

	def test_gateway(self):
		out = self.talk(None, b'GET /http://127.0.0.1:%d/g HTTP/1.0\r\n\r\n' % self.oport)
		self.assertIn('Hello from /g', out)
		self.assertIn('href="http://127.0.0.1:%d/http://127.0.0.1:%d/next"' % (self.pport, self.oport), out)

	def test_pac_and_home(self):
		self.assertIn('FindProxyForURL', self.talk(None, b'GET /proxy.pac HTTP/1.0\r\n\r\n'))
		self.assertIn('LegacyProxy', self.talk(None, b'GET / HTTP/1.0\r\n\r\n'))


if __name__ == '__main__':
	unittest.main()
