"""The proxy's own pages, written in HTML 2.0 so even Mosaic renders them."""

from . import __version__
from .text import to_legacy_html


def _page(title, body):
	return ('<html>\n<head>\n<title>%s</title>\n</head>\n<body bgcolor="#ffffff">\n'
		'<h1>%s</h1>\n%s\n<hr>\n<address>LegacyProxy %s</address>\n</body>\n</html>\n') % (
		to_legacy_html(title), to_legacy_html(title), body, __version__)


def pac_file(hostport):
	"""Proxy auto-config for Netscape 2+ and IE 3+. Plain string tests only: isInNet() makes old
	machines resolve every host name, and JavaScript 1.0 lacks most of the language."""
	return '''function FindProxyForURL(url, host)
{
	if (isPlainHostName(host) || host == "localhost" || shExpMatch(host, "127.*") ||
		shExpMatch(host, "10.*") || shExpMatch(host, "192.168.*") || shExpMatch(host, "169.254.*") ||
		shExpMatch(host, "172.1[6-9].*") || shExpMatch(host, "172.2[0-9].*") || shExpMatch(host, "172.3[01].*") ||
		shExpMatch(host, "*.local"))
		return "DIRECT";
	if (shExpMatch(url, "http:*") || shExpMatch(url, "https:*"))
		return "PROXY %s";
	return "DIRECT";
}
''' % hostport


def error_page(title, message, url=None):
	body = '<p>%s</p>\n' % to_legacy_html(message)
	if url:
		body += '<p>URL: <code>%s</code></p>\n' % to_legacy_html(url)
	return _page(title, body)


def home_page(profile, settings, base, image_backend, mode):
	e = to_legacy_html
	host = base.split('//', 1)[-1].rstrip('/')
	rows = [
		('Your browser', '%s (%s)' % (profile.name, profile.key)),
		('You are using', {'proxy': 'HTTP proxy mode', 'socks': 'SOCKS mode',
			'gateway': 'prefix (gateway) mode'}.get(mode, mode)),
		('Images', 'shrunk to %d px / %d KB with %s' % (settings.image_max_size, settings.image_max_kb,
			image_backend.name if image_backend else 'nothing (GIF/JPEG passed through, others refused)')),
		('Upstream TLS', settings.tls_description),
	]
	info = '<dl>\n' + ''.join('<dt><b>%s</b>\n<dd>%s\n' % (e(k), e(v)) for k, v in rows) + '</dl>\n'
	return _page('LegacyProxy', '''
<form action="%(base)s/go" method="get">
<p>Go to: <input type="text" name="url" size="40" value="http://">
<input type="submit" value="Go"></p>
</form>
%(info)s
<h2>Setting up your browser</h2>
<dl>
<dt><b>HTTP proxy</b> (Netscape, Internet Explorer, OmniWeb, Internet Config on Mac OS 8/9)
<dd>Set the HTTP proxy to <code>%(host_only)s</code> port <code>%(port)s</code>, then type ordinary
addresses. Also set the FTP and Gopher proxies to it if you like. Leave the Security/SSL proxy empty and type
<code>http://</code> even for secure sites: the proxy upgrades to HTTPS for you.
<dt><b>Automatic proxy configuration</b> (Netscape 2+, Internet Explorer 3+)
<dd>Use <code>%(base)s/proxy.pac</code> as the configuration URL. Addresses on your local network go direct;
everything else goes through the proxy.
<dt><b>SOCKS</b> (Internet Config, Netscape, Internet Explorer)
<dd>Set the SOCKS host to <code>%(host_only)s</code> port <code>%(port)s</code>. Web traffic on port 80 gets the
same treatment as the HTTP proxy; everything else is passed straight through.
<dt><b>No proxy settings</b> (NCSA Mosaic, anything else)
<dd>Use the box above or put the proxy in front of any address:
<code>%(base)s/http://www.example.com/</code>. Every link you follow stays on the proxy.
Mosaic's WAIS gateway can also point at <code>%(host)s</code>: then typing <code>wais://www.example.com/</code>
opens that site through the proxy.
</dl>
''' % {'base': e(base), 'host': e(host), 'host_only': e(host.rsplit(':', 1)[0]),
		'port': e(host.rsplit(':', 1)[1] if ':' in host else '80'), 'info': info})
