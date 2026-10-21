"""Command line entry point: python3 -m legacyproxy [options]"""

import argparse
import os
import shutil
import ssl
import subprocess
import sys

from . import __version__, browsers
from .upstream import USER_AGENT, tls13_available

# Pythons that usually link a modern OpenSSL, tried when the running one cannot do TLS 1.3
# (Apple's /usr/bin/python3 uses LibreSSL 2.8.3).
_CANDIDATES = [
	'/opt/homebrew/bin/python3', '/usr/local/bin/python3',
	'/Library/Frameworks/Python.framework/Versions/Current/bin/python3',
	'~/.local/bin/python3', '/opt/local/bin/python3',
]
_CHECK = 'import ssl,sys; sys.exit(0 if ssl.HAS_TLSv1_3 and sys.version_info >= (3, 9) else 1)'


def better_python():
	seen = {os.path.realpath(sys.executable)}
	names = [os.path.expanduser(c) for c in _CANDIDATES]
	names += [shutil.which('python3.%d' % v) for v in range(20, 8, -1)]
	for c in names:
		if not c or not os.path.exists(c) or os.path.realpath(c) in seen:
			continue
		seen.add(os.path.realpath(c))
		try:
			if subprocess.run([c, '-c', _CHECK], timeout=10).returncode == 0:
				return c
		except (OSError, subprocess.SubprocessError):
			continue
	return None


def ensure_tls(args):
	if tls13_available() or args.allow_old_tls or os.environ.get('LEGACYPROXY_REEXEC'):
		return
	py = better_python()
	if py:
		print('This Python (%s, %s) has no TLS 1.3; restarting under %s' % (sys.executable, ssl.OPENSSL_VERSION, py))
		sys.stdout.flush()
		root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		env = dict(os.environ, LEGACYPROXY_REEXEC='1',
			PYTHONPATH=root + os.pathsep + os.environ.get('PYTHONPATH', ''))
		os.execve(py, [py, '-m', 'legacyproxy'] + sys.argv[1:], env)
	sys.exit('This Python (%s) is linked against %s, which cannot speak TLS 1.3, so many sites would fail.\n'
		'Install a current Python (macOS: "brew install python", or the python.org installer) and run\n'
		'the proxy again; it will find it automatically. Use --allow-old-tls to run anyway.'
		% (sys.executable, ssl.OPENSSL_VERSION))


def listen_spec(value):
	host, _, port = value.rpartition(':')
	try:
		return host.strip('[]'), int(port)
	except ValueError:
		raise argparse.ArgumentTypeError('expected [host:]port, got %r' % value)


def main(argv=None):
	ap = argparse.ArgumentParser(prog='legacyproxy',
		description='HTTP, SOCKS and URL-prefix proxy that makes the modern web usable from vintage browsers.')
	ap.add_argument('-l', '--listen', action='append', type=listen_spec, metavar='[HOST:]PORT',
		help='address to listen on; repeatable (default 8080; every port speaks HTTP proxy, SOCKS and gateway)')
	ap.add_argument('-v', '--verbose', action='store_true', help='log request details')
	ap.add_argument('--image-max-size', type=int, default=480, metavar='PX',
		help='longest side of converted images (default 480)')
	ap.add_argument('--image-max-kb', type=int, default=8, metavar='KB',
		help='target size of converted images (default 8)')
	ap.add_argument('--image-backend', choices=['pillow', 'sips', 'magick'], help='force an image converter')
	ap.add_argument('--no-images', action='store_true', help='pass images through unconverted')
	ap.add_argument('--no-css', action='store_true', help="don't fetch stylesheets (faster, cruder pages)")
	ap.add_argument('--browser', choices=sorted(browsers.PROFILES), help='ignore User-Agent and use this profile')
	ap.add_argument('--default-browser', choices=sorted(browsers.PROFILES), default='netscape3',
		help='profile for unrecognised browsers (default netscape3)')
	ap.add_argument('--viewport', type=int, default=800, metavar='PX',
		help='screen width assumed for responsive CSS (default 800)')
	ap.add_argument('--user-agent', default=USER_AGENT, help='User-Agent sent to web sites (default: current Chrome)')
	ap.add_argument('--no-upgrade', action='store_true', help="don't try https:// first for plain http:// requests")
	ap.add_argument('--insecure', action='store_true', help="don't verify web sites' TLS certificates")
	ap.add_argument('--allow', action='append', metavar='CIDR',
		help='client networks allowed to connect; repeatable (default: private and loopback ranges)')
	ap.add_argument('--allow-old-tls', action='store_true', help='run even if this Python lacks TLS 1.3')
	ap.add_argument('--version', action='version', version='LegacyProxy ' + __version__)
	args = ap.parse_args(argv)

	ensure_tls(args)

	from .server import DEFAULT_ALLOW, Proxy, Settings
	settings = Settings(
		listen=args.listen or [('', 8080)],
		verbose=args.verbose,
		image_max_size=args.image_max_size,
		image_max_kb=args.image_max_kb,
		image_backend=args.image_backend,
		images=not args.no_images,
		css=not args.no_css,
		force_profile=args.browser,
		default_profile=args.default_browser,
		viewport=args.viewport,
		user_agent=args.user_agent,
		upgrade=not args.no_upgrade,
		insecure=args.insecure,
		allow=args.allow or DEFAULT_ALLOW,
		tls_description='%s%s' % (ssl.OPENSSL_VERSION, '' if tls13_available() else ' (no TLS 1.3!)'),
	)
	proxy = Proxy(settings)
	backend = proxy.image_backend.name if proxy.image_backend else 'none (install Pillow or ImageMagick)'
	print('LegacyProxy %s on Python %s, %s, images: %s' % (__version__, sys.version.split()[0],
		settings.tls_description, backend if settings.images else 'off'))
	for host, port in settings.listen:
		print('Listening on %s:%d (HTTP proxy, SOCKS 4/5, and http://<this host>:%d/http://... prefix mode)'
			% (host or '*', port, port))
	print('Press ^C to exit.')
	sys.stdout.flush()
	proxy.serve_forever()


if __name__ == '__main__':
	main()
