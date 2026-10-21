"""Browser detection and capability profiles."""

import re


class Profile:
	"""What a family of browsers can render.

	tables        understands <table>
	body_colors   understands <body bgcolor text link vlink>
	cell_bgcolor  understands bgcolor on table cells
	font_color    understands <font color>
	center        understands <center>
	div           understands <div> (and align on it)
	nbsp          understands &nbsp;
	latin1_symbols renders Latin-1 symbol entities (&copy; &frac12; ...)
	jpeg          shows JPEG images inline (otherwise everything becomes GIF)
	iframe        understands <iframe>
	button        understands <button>
	css           None, 'ns4' (tiny, careful subset) or 'ie' (CSS1-ish subset)
	"""

	DEFAULTS = dict(tables=True, body_colors=True, cell_bgcolor=True, font_color=True, center=True,
		div=True, nbsp=True, latin1_symbols=True, jpeg=True, iframe=False, button=False, css=None)

	def __init__(self, key, name, **caps):
		self.key = key
		self.name = name
		for k, v in self.DEFAULTS.items():
			setattr(self, k, caps.pop(k, v))
		if caps:
			raise TypeError('unknown capabilities: %s' % ', '.join(caps))

	def __repr__(self):
		return '<Profile %s>' % self.key


_HTML2 = dict(tables=False, body_colors=False, cell_bgcolor=False, font_color=False, center=False,
	div=False, nbsp=False, latin1_symbols=False, jpeg=False)

PROFILES = {p.key: p for p in [
	Profile('mosaic', 'NCSA Mosaic', **_HTML2),
	Profile('mosaic3', 'NCSA Mosaic 3', **dict(_HTML2, tables=True, center=True, jpeg=True)),
	Profile('netscape1', 'Netscape 1.0', **dict(_HTML2, center=True, jpeg=True)),
	Profile('netscape1.1', 'Netscape 1.1', **dict(_HTML2, tables=True, body_colors=True, center=True, nbsp=True, jpeg=True)),
	Profile('netscape2', 'Netscape 2', cell_bgcolor=False),
	Profile('netscape3', 'Netscape 3'),
	Profile('netscape4', 'Netscape 4', css='ns4'),
	Profile('ie3', 'Internet Explorer 3', iframe=True),
	Profile('ie4', 'Internet Explorer 4', iframe=True, button=True, css='ie'),
	Profile('ie5', 'Internet Explorer 5+', iframe=True, button=True, css='ie'),
	Profile('omniweb', 'OmniWeb (NeXT/OpenStep)'),
	Profile('omniweb4', 'OmniWeb 4+', iframe=True, button=True, css='ie'),
	Profile('modern', 'Modern browser', iframe=True, button=True, css='ie'),
]}


def _version(pattern, ua):
	m = re.search(pattern, ua)
	if not m:
		return None
	try:
		return float(m.group(1))
	except ValueError:
		return None


def detect(user_agent, default='netscape3'):
	"""Pick a profile from a User-Agent header."""
	ua = user_agent or ''
	key = default
	if 'Mosaic' in ua:
		v = _version(r'Mosaic[/ ](\d+(?:\.\d+)?)', ua) or 2
		key = 'mosaic3' if v >= 3 else 'mosaic'
	elif 'Lynx' in ua or 'MacWeb' in ua or 'WannaBe' in ua:
		key = 'mosaic'
	elif 'OmniWeb' in ua:
		v = _version(r'OmniWeb/v?(\d+(?:\.\d+)?)', ua) or 3
		key = 'omniweb4' if v >= 4 else 'omniweb'
	elif 'Cyberdog' in ua:
		key = 'netscape2'
	elif 'MSIE' in ua:
		v = _version(r'MSIE (\d+(?:\.\d+)?)', ua) or 3
		key = 'ie3' if v < 4 else 'ie4' if v < 5 else 'ie5'
	elif 'Opera' in ua:
		v = _version(r'Opera[/ ](\d+(?:\.\d+)?)', ua) or 3
		key = 'netscape3' if v < 4 else 'netscape4' if v < 5 else 'ie5'
	elif 'iCab' in ua:
		key = 'netscape3'
	elif ua.startswith('Mozilla/'):
		v = _version(r'^Mozilla/(\d+(?:\.\d+)?)', ua) or 3
		if v < 1.1:
			key = 'netscape1'
		elif v < 2:
			key = 'netscape1.1'
		elif v < 3:
			key = 'netscape2'
		elif v < 4:
			key = 'netscape3'
		elif v < 5:
			key = 'netscape4'
		else:
			key = 'modern'
	return PROFILES.get(key, PROFILES['netscape3'])
