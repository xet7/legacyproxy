"""A small CSS engine: enough parsing, matching and cascading to translate modern
stylesheets into HTML 3.2 attributes, or into a CSS1 subset old browsers survive."""

import math
import re

# ---------------------------------------------------------------- colors

_NAMED = (
	'aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff beige f5f5dc bisque ffe4c4 '
	'black 000000 blanchedalmond ffebcd blue 0000ff blueviolet 8a2be2 brown a52a2a burlywood deb887 cadetblue 5f9ea0 '
	'chartreuse 7fff00 chocolate d2691e coral ff7f50 cornflowerblue 6495ed cornsilk fff8dc crimson dc143c cyan 00ffff '
	'darkblue 00008b darkcyan 008b8b darkgoldenrod b8860b darkgray a9a9a9 darkgrey a9a9a9 darkgreen 006400 '
	'darkkhaki bdb76b darkmagenta 8b008b darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc darkred 8b0000 '
	'darksalmon e9967a darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f darkslategrey 2f4f4f '
	'darkturquoise 00ced1 darkviolet 9400d3 deeppink ff1493 deepskyblue 00bfff dimgray 696969 dimgrey 696969 '
	'dodgerblue 1e90ff firebrick b22222 floralwhite fffaf0 forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc '
	'ghostwhite f8f8ff gold ffd700 goldenrod daa520 gray 808080 grey 808080 green 008000 greenyellow adff2f '
	'honeydew f0fff0 hotpink ff69b4 indianred cd5c5c indigo 4b0082 ivory fffff0 khaki f0e68c lavender e6e6fa '
	'lavenderblush fff0f5 lawngreen 7cfc00 lemonchiffon fffacd lightblue add8e6 lightcoral f08080 lightcyan e0ffff '
	'lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgrey d3d3d3 lightgreen 90ee90 lightpink ffb6c1 '
	'lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa lightslategray 778899 lightslategrey 778899 '
	'lightsteelblue b0c4de lightyellow ffffe0 lime 00ff00 limegreen 32cd32 linen faf0e6 magenta ff00ff maroon 800000 '
	'mediumaquamarine 66cdaa mediumblue 0000cd mediumorchid ba55d3 mediumpurple 9370db mediumseagreen 3cb371 '
	'mediumslateblue 7b68ee mediumspringgreen 00fa9a mediumturquoise 48d1cc mediumvioletred c71585 '
	'midnightblue 191970 mintcream f5fffa mistyrose ffe4e1 moccasin ffe4b5 navajowhite ffdead navy 000080 '
	'oldlace fdf5e6 olive 808000 olivedrab 6b8e23 orange ffa500 orangered ff4500 orchid da70d6 palegoldenrod eee8aa '
	'palegreen 98fb98 paleturquoise afeeee palevioletred db7093 papayawhip ffefd5 peachpuff ffdab9 peru cd853f '
	'pink ffc0cb plum dda0dd powderblue b0e0e6 purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f '
	'royalblue 4169e1 saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57 seashell fff5ee '
	'sienna a0522d silver c0c0c0 skyblue 87ceeb slateblue 6a5acd slategray 708090 slategrey 708090 snow fffafa '
	'springgreen 00ff7f steelblue 4682b4 tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347 turquoise 40e0d0 '
	'violet ee82ee wheat f5deb3 white ffffff whitesmoke f5f5f5 yellow ffff00 yellowgreen 9acd32 '
	'canvas ffffff canvastext 000000 linktext 0000ee buttonface dddddd buttontext 000000 field ffffff fieldtext 000000'
).split()
NAMED_COLORS = {_NAMED[i]: '#' + _NAMED[i + 1] for i in range(0, len(_NAMED), 2)}

_FUNC_COLOR = re.compile(r'^(rgba?|hsla?|oklch|oklab|hwb)\((.*)\)$', re.S)


def _channel(tok, scale):
	"""Parse a number or percentage; percentages are relative to `scale`."""
	tok = tok.strip()
	if tok == 'none':
		return 0.0
	if tok.endswith('%'):
		return float(tok[:-1]) * scale / 100.0
	return float(tok)


def _hue(tok):
	tok = tok.strip()
	for unit, mult in (('deg', 1), ('grad', 0.9), ('rad', 180 / math.pi), ('turn', 360)):
		if tok.endswith(unit):
			return float(tok[:-len(unit)]) * mult
	return 0.0 if tok == 'none' else float(tok)


def _srgb_gamma(c):
	c = max(0.0, min(1.0, c))
	return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _oklab_to_rgb(L, a, b):
	l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
	m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
	s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
	r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
	g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
	bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
	return tuple(_srgb_gamma(x) * 255 for x in (r, g, bl))


def _hsl_to_rgb(h, s, l):
	h = (h % 360) / 360.0

	def f(n):
		k = (n + h * 12) % 12
		return l - s * min(l, 1 - l) * max(-1, min(k - 3, 9 - k, 1))
	return f(0) * 255, f(8) * 255, f(4) * 255


def parse_color(value):
	"""CSS color -> '#rrggbb', or None for transparent / unknown / mostly transparent."""
	if not value:
		return None
	v = value.strip().lower()
	if v.endswith('!important'):
		v = v[:-10].strip()
	if v.startswith('#'):
		h = v[1:]
		if len(h) in (3, 4) and all(c in '0123456789abcdef' for c in h):
			if len(h) == 4 and int(h[3], 16) < 8:
				return None
			return '#' + ''.join(c * 2 for c in h[:3])
		if len(h) in (6, 8) and all(c in '0123456789abcdef' for c in h):
			if len(h) == 8 and int(h[6:], 16) < 128:
				return None
			return '#' + h[:6]
		return None
	if v in NAMED_COLORS:
		return NAMED_COLORS[v]
	m = _FUNC_COLOR.match(v)
	if not m:
		return None
	fn, args = m.groups()
	args = args.replace(',', ' ').replace('/', ' / ')
	parts = args.split()
	alpha = 1.0
	if '/' in parts:
		i = parts.index('/')
		try:
			alpha = _channel(parts[i + 1], 1.0)
		except (ValueError, IndexError):
			return None
		parts = parts[:i]
	elif len(parts) == 4:
		try:
			alpha = _channel(parts[3], 1.0)
		except ValueError:
			return None
		parts = parts[:3]
	if len(parts) != 3 or alpha < 0.5:
		return None
	try:
		if fn in ('rgb', 'rgba'):
			rgb = [_channel(p, 255) for p in parts]
		elif fn in ('hsl', 'hsla'):
			rgb = _hsl_to_rgb(_hue(parts[0]), _channel(parts[1], 1) / (1 if parts[1].endswith('%') else 100),
				_channel(parts[2], 1) / (1 if parts[2].endswith('%') else 100))
		elif fn == 'hwb':
			w, bk = _channel(parts[1], 1), _channel(parts[2], 1)
			r, g, b = _hsl_to_rgb(_hue(parts[0]), 1, 0.5)
			rgb = [(c / 255 * (1 - w - bk) + w) * 255 for c in (r, g, b)]
		elif fn == 'oklab':
			rgb = _oklab_to_rgb(_channel(parts[0], 1), _channel(parts[1], 0.4), _channel(parts[2], 0.4))
		else:  # oklch
			L, C, H = _channel(parts[0], 1), _channel(parts[1], 0.4), _hue(parts[2])
			rgb = _oklab_to_rgb(L, C * math.cos(math.radians(H)), C * math.sin(math.radians(H)))
	except (ValueError, ZeroDivisionError, OverflowError):
		return None
	return '#%02x%02x%02x' % tuple(int(round(max(0, min(255, c)))) for c in rgb)


def luminance(hexcolor):
	def lin(c):
		c /= 255.0
		return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
	r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
	return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(c1, c2):
	l1, l2 = luminance(c1), luminance(c2)
	if l1 < l2:
		l1, l2 = l2, l1
	return (l1 + 0.05) / (l2 + 0.05)


# ---------------------------------------------------------------- low-level scanning

def _skip_string(s, i):
	q = s[i]
	i += 1
	while i < len(s):
		if s[i] == '\\':
			i += 2
			continue
		if s[i] == q:
			return i + 1
		i += 1
	return i


def _find_block_end(s, i):
	"""s[i] == '{'; return index just past the matching '}'."""
	depth = 0
	n = len(s)
	while i < n:
		c = s[i]
		if c in '"\'':
			i = _skip_string(s, i)
			continue
		if c == '{':
			depth += 1
		elif c == '}':
			depth -= 1
			if depth == 0:
				return i + 1
		i += 1
	return n


def _find_any(s, i, chars):
	"""Index of the first char in `chars` outside strings and parens, or len(s)."""
	depth = 0
	n = len(s)
	while i < n:
		c = s[i]
		if c in '"\'':
			i = _skip_string(s, i)
			continue
		if c == '(':
			depth += 1
		elif c == ')':
			depth = max(0, depth - 1)
		elif depth == 0 and c in chars:
			return i
		i += 1
	return n


def split_top(s, sep):
	"""Split on `sep` outside strings and parentheses."""
	out = []
	i = 0
	while True:
		j = _find_any(s, i, sep)
		out.append(s[i:j])
		if j >= len(s):
			return out
		i = j + 1


_COMMENT = re.compile(r'/\*.*?\*/', re.S)
_IMPORTANT = re.compile(r'\s*!\s*important\s*$', re.I)


def parse_declarations(body):
	"""'a: b; c: d !important' -> [(prop, value, important)]; nested rules are skipped."""
	decls = []
	i = 0
	n = len(body)
	while i < n:
		j = _find_any(body, i, ';{')
		if j < n and body[j] == '{':
			i = _find_block_end(body, j)  # CSS nesting: ignore nested rule
			continue
		chunk = body[i:j]
		i = j + 1
		if ':' not in chunk:
			continue
		prop, value = chunk.split(':', 1)
		prop = prop.strip().lower()
		value = value.strip()
		if not prop or not value:
			continue
		important = False
		m = _IMPORTANT.search(value)
		if m:
			important = True
			value = value[:m.start()].strip()
		if not prop.startswith('--'):
			prop = prop.lower()
		decls.append((prop, value, important))
	return decls


# ---------------------------------------------------------------- media queries

_LEN = re.compile(r'^(-?[\d.]+)(px|em|rem)?$')


def _px(tok):
	m = _LEN.match(tok.strip())
	if not m:
		return None
	n = float(m.group(1))
	return n * 16 if m.group(2) in ('em', 'rem') else n


def media_matches(query, width=800):
	"""Evaluate a media query list for a desktop screen `width` px wide in light mode."""
	q = query.strip().lower()
	if not q:
		return True
	for part in split_top(q, ','):
		if _media_one(part.strip(), width):
			return True
	return False


def _media_one(q, width):
	negate = False
	if q.startswith('not '):
		negate, q = True, q[4:].strip()
	elif q.startswith('only '):
		q = q[5:].strip()
	ok = True
	for term in re.split(r'\s+and\s+', q):
		term = term.strip()
		if not term:
			continue
		if term.startswith('('):
			ok = ok and _media_feature(term.strip('()').strip(), width)
		else:
			ok = ok and term in ('screen', 'all')
	return ok != negate


def _media_feature(f, width):
	m = re.match(r'^(-?[\d.]+(?:px|em|rem)?)\s*(<=|>=|<|>)\s*width\s*(?:(<=|>=|<|>)\s*(-?[\d.]+(?:px|em|rem)?))?$', f)
	if m:  # 400px <= width <= 800px
		lo = _px(m.group(1))
		ok = _cmp(lo, {'<': '>', '<=': '>=', '>': '<', '>=': '<='}[m.group(2)], width)
		if m.group(3):
			ok = ok and _cmp(width, m.group(3), _px(m.group(4)))
		return ok
	m = re.match(r'^width\s*(<=|>=|<|>|=)\s*(-?[\d.]+(?:px|em|rem)?)$', f)
	if m:
		return _cmp(width, m.group(1), _px(m.group(2)))
	if ':' not in f:
		return f in ('color', 'hover', 'grid')
	name, val = (x.strip() for x in f.split(':', 1))
	px = _px(val)
	if name in ('min-width', 'min-device-width'):
		return px is not None and width >= px
	if name in ('max-width', 'max-device-width'):
		return px is not None and width <= px
	if name == 'width':
		return px is not None and width == px
	if name in ('min-height', 'min-device-height'):
		return px is not None and px <= 600
	if name in ('max-height', 'max-device-height'):
		return px is not None and px >= 600
	if name == 'prefers-color-scheme':
		return val == 'light'
	if name == 'prefers-reduced-motion':
		return val == 'reduce'  # we have no motion at all
	if name in ('hover', 'any-hover'):
		return val == 'hover'
	if name in ('pointer', 'any-pointer'):
		return val == 'fine'
	if name == 'orientation':
		return val == 'landscape'
	if name in ('-webkit-min-device-pixel-ratio', 'min-resolution', 'min--moz-device-pixel-ratio'):
		return (_px(val.replace('dppx', '').replace('x', '')) or 99) <= 1
	if name in ('-webkit-max-device-pixel-ratio', 'max-resolution'):
		return True
	return False


def _cmp(a, op, b):
	if a is None or b is None:
		return False
	return {'<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b, '=': a == b}[op]


# ---------------------------------------------------------------- selectors

_SEL_TOKEN = re.compile(r'''
	(?P<comb>\s*[>+~]\s*|\s+)
	| (?P<id>\#-?[_a-zA-Z -￿][\w -￿-]*)
	| (?P<cls>\.-?[_a-zA-Z -￿][\w -￿-]*)
	| (?P<attr>\[\s*[\w-]+\s*(?:[~|^$*]?=\s*(?:"[^"]*"|'[^']*'|[^\]\s]+)\s*(?:\s[is])?)?\s*\])
	| (?P<pseudo>::?[\w-]+(?:\([^()]*\))?)
	| (?P<tag>\*|[a-zA-Z][\w-]*)
''', re.X)

_ATTR_PARTS = re.compile(r'''\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*("[^"]*"|'[^']*'|[^\]\s]+))?''')

# pseudo-classes that hold in the static, never-interacted-with state we render
STATIC_PSEUDOS = {'root', 'link', 'any-link'}


class Compound:
	__slots__ = ('tag', 'id', 'classes', 'attrs', 'pseudos')

	def __init__(self):
		self.tag = None
		self.id = None
		self.classes = []
		self.attrs = []
		self.pseudos = []


class Selector:
	"""A complex selector stored right-to-left: parts[0] is the subject."""
	__slots__ = ('parts', 'specificity', 'text')

	def __init__(self, parts, specificity, text):
		self.parts = parts
		self.specificity = specificity
		self.text = text


def parse_selector(text):
	"""Parse one complex selector, or return None if we can't (or shouldn't) match it."""
	text = text.strip()
	if not text or '\\' in text:
		return None
	compounds = []  # left to right: (Compound, combinator-before-it)
	cur = Compound()
	empty = True
	comb = None
	pos = 0
	while pos < len(text):
		m = _SEL_TOKEN.match(text, pos)
		if not m:
			return None
		pos = m.end()
		kind = m.lastgroup
		val = m.group(kind)
		if kind == 'comb':
			if empty:
				if val.strip():
					return None  # leading combinator
				continue
			compounds.append((cur, comb))
			cur, empty = Compound(), True
			comb = val.strip() or ' '
			if comb in '+~':
				return None  # sibling combinators: not supported
			continue
		empty = False
		if kind == 'id':
			cur.id = val[1:]
		elif kind == 'cls':
			cur.classes.append(val[1:])
		elif kind == 'tag':
			cur.tag = None if val == '*' else val.lower()
		elif kind == 'attr':
			am = _ATTR_PARTS.match(val)
			if not am:
				return None
			name, op, v = am.groups()
			if v and v[0] in '"\'':
				v = v[1:-1]
			cur.attrs.append((name.lower(), op, v))
		elif kind == 'pseudo':
			if val.startswith('::'):
				return None
			name = val[1:].split('(')[0].lower()
			if name in ('before', 'after', 'first-line', 'first-letter', 'placeholder', 'selection', 'marker'):
				return None
			cur.pseudos.append(name)
	if empty:
		return None
	compounds.append((cur, comb))
	parts = []
	ids = cls = tags = 0
	for i in range(len(compounds) - 1, -1, -1):
		c, _ = compounds[i]
		left_comb = compounds[i][1]
		parts.append((c, left_comb))
		ids += 1 if c.id else 0
		cls += len(c.classes) + len(c.attrs) + len(c.pseudos)
		tags += 1 if c.tag else 0
	return Selector(parts, (ids, cls, tags), text)


def _match_attr(node, name, op, val):
	have = node.attrs.get(name)
	if have is None:
		return False
	if op is None:
		return True
	if op == '=':
		return have == val
	if op == '~=':
		return val in have.split()
	if op == '|=':
		return have == val or have.startswith(val + '-')
	if op == '^=':
		return bool(val) and have.startswith(val)
	if op == '$=':
		return bool(val) and have.endswith(val)
	if op == '*=':
		return bool(val) and val in have
	return False


def _match_compound(node, c):
	if c.tag and node.tag != c.tag:
		return False
	if c.id and node.attrs.get('id') != c.id:
		return False
	if c.classes:
		nc = node.classes
		for k in c.classes:
			if k not in nc:
				return False
	for name, op, val in c.attrs:
		if not _match_attr(node, name, op, val):
			return False
	for p in c.pseudos:
		if p == 'root':
			if node.tag != 'html':
				return False
		elif p in ('link', 'any-link'):
			if node.tag not in ('a', 'area') or 'href' not in node.attrs:
				return False
		else:
			return False  # :hover, :focus, :checked, :not(...) etc. never match statically
	return True


def matches(node, sel, i=0):
	comp, comb = sel.parts[i]
	if not _match_compound(node, comp):
		return False
	if i + 1 == len(sel.parts):
		return True
	p = node.parent
	if comb == '>':
		return p is not None and p.tag != '#document' and matches(p, sel, i + 1)
	while p is not None and p.tag != '#document':
		if matches(p, sel, i + 1):
			return True
		p = p.parent
	return False


# ---------------------------------------------------------------- stylesheets

class Rule:
	__slots__ = ('selectors', 'decls', 'order', 'selector_text')

	def __init__(self, selectors, decls, order, selector_text):
		self.selectors = selectors
		self.decls = decls
		self.order = order
		self.selector_text = selector_text


class Sheet:
	def __init__(self):
		self.rules = []       # Rule, in source order
		self.vars = {}        # custom properties defined on :root/html/body
		self.imports = []     # (url, media) still to be fetched


_VAR_HOSTS = {':root', 'html', 'body', '*', ':host', 'html:root'}
_counter = [0]


def parse_stylesheet(text, width=800, sheet=None):
	"""Parse CSS text, flattening @media/@layer blocks that apply to a desktop screen."""
	sheet = sheet or Sheet()
	text = _COMMENT.sub('', text)
	_parse_block(text, sheet, width)
	return sheet


def _parse_block(s, sheet, width):
	i = 0
	n = len(s)
	while i < n:
		while i < n and s[i] in ' \t\r\n;':
			i += 1
		if i >= n:
			break
		if s[i] == '}':  # stray close brace
			i += 1
			continue
		j = _find_any(s, i, '{;')
		prelude = s[i:j].strip()
		if j >= n or s[j] == ';':
			if prelude.lower().startswith('@import'):
				_parse_import(prelude, sheet)
			i = j + 1
			continue
		end = _find_block_end(s, j)
		body = s[j + 1:end - 1]
		i = end
		if prelude.startswith('@'):
			kw = prelude.split(None, 1)
			name = kw[0].lower()
			rest = kw[1] if len(kw) > 1 else ''
			if name == '@media':
				if media_matches(rest, width):
					_parse_block(body, sheet, width)
			elif name == '@layer':
				_parse_block(body, sheet, width)
			elif name == '@supports':
				if rest.strip().lower().startswith('not'):
					_parse_block(body, sheet, width)
			# @font-face, @keyframes, @container, @page, @property ...: ignored
			continue
		decls = parse_declarations(body)
		if not decls:
			continue
		sel_texts = [t.strip() for t in split_top(prelude, ',') if t.strip()]
		custom = [d for d in decls if d[0].startswith('--')]
		if custom and any(t in _VAR_HOSTS for t in sel_texts):
			for prop, value, _ in custom:
				sheet.vars[prop] = value
		decls = [d for d in decls if not d[0].startswith('--')]
		if not decls:
			continue
		selectors = [sel for sel in (parse_selector(t) for t in sel_texts) if sel]
		_counter[0] += 1
		sheet.rules.append(Rule(selectors, decls, _counter[0], sel_texts))


def _parse_import(prelude, sheet):
	m = re.match(r'''@import\s+(?:url\(\s*)?["']?([^"')\s]+)["']?\s*\)?\s*(.*)$''', prelude, re.I | re.S)
	if m:
		media = m.group(2).strip()
		if media.lower().startswith(('layer', 'supports')):
			media = ''
		sheet.imports.append((m.group(1), media))


_VAR = re.compile(r'var\(\s*(--[\w-]+)\s*(?:,\s*)?')


def resolve_vars(value, variables, depth=0):
	if 'var(' not in value or depth > 8:
		return value
	out = []
	i = 0
	while True:
		m = _VAR.search(value, i)
		if not m:
			out.append(value[i:])
			break
		out.append(value[i:m.start()])
		# find the closing paren of this var(
		depth_p = 1
		j = m.end()
		while j < len(value) and depth_p:
			if value[j] == '(':
				depth_p += 1
			elif value[j] == ')':
				depth_p -= 1
			j += 1
		fallback = value[m.end():j - 1].strip()
		repl = variables.get(m.group(1))
		if repl is None:
			repl = fallback
		out.append(resolve_vars(repl, variables, depth + 1))
		i = j
	return ''.join(out)


class StyleEngine:
	"""Computes the declared (not inherited) values of a few properties for tree nodes."""

	def __init__(self, sheets):
		self.vars = {}
		self.by_id = {}
		self.by_class = {}
		self.by_tag = {}
		self.universal = []
		self.sheets = sheets
		for sheet in sheets:
			self.vars.update(sheet.vars)
			for rule in sheet.rules:
				for sel in rule.selectors:
					c = sel.parts[0][0]
					entry = (sel, rule)
					if c.id:
						self.by_id.setdefault(c.id, []).append(entry)
					elif c.classes:
						self.by_class.setdefault(c.classes[0], []).append(entry)
					elif c.tag:
						self.by_tag.setdefault(c.tag, []).append(entry)
					else:
						self.universal.append(entry)

	def _candidates(self, node):
		out = list(self.universal)
		ident = node.attrs.get('id')
		if ident and ident in self.by_id:
			out.extend(self.by_id[ident])
		for c in node.classes:
			if c in self.by_class:
				out.extend(self.by_class[c])
		if node.tag in self.by_tag:
			out.extend(self.by_tag[node.tag])
		return out

	def computed(self, node):
		"""Return {property: value} from matching rules and the style attribute."""
		hits = []
		for sel, rule in self._candidates(node):
			if matches(node, sel):
				hits.append((sel.specificity, rule.order, rule))
		hits.sort(key=lambda h: (h[0], h[1]))
		normal = {}
		important = {}
		for _, _, rule in hits:
			for prop, value, imp in rule.decls:
				(important if imp else normal)[prop] = value
		inline = node.attrs.get('style')
		if inline:
			for prop, value, imp in parse_declarations(inline):
				if prop.startswith('--'):
					continue
				(important if imp else normal)[prop] = value
		normal.update(important)
		if self.vars:
			for k, v in normal.items():
				if 'var(' in v:
					normal[k] = resolve_vars(v, self.vars)
		return normal


# ---------------------------------------------------------------- downlevel output

_SIMPLE_COMPOUND = r'(?:[a-z][a-z0-9]*)?(?:[.#][_a-zA-Z][\w-]*)*'
_OUT_SEL = re.compile(r'^%s(?:\s+%s){0,3}$' % (_SIMPLE_COMPOUND, _SIMPLE_COMPOUND))
_OUT_SEL_LINK = re.compile(r'^(?:%s\s+)*a(?:[.#][_a-zA-Z][\w-]*)*:(link|visited|hover|active)$' % _SIMPLE_COMPOUND)

_PROPS = {
	'ns4': {'color', 'background-color', 'font-family', 'font-size', 'font-weight', 'font-style',
		'text-align', 'text-decoration', 'text-transform'},
	'ie': {'color', 'background-color', 'font-family', 'font-size', 'font-weight', 'font-style',
		'text-align', 'text-decoration', 'text-transform', 'text-indent', 'line-height', 'letter-spacing',
		'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
		'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
		'border', 'border-top', 'border-right', 'border-bottom', 'border-left', 'border-color', 'border-style', 'border-width',
		'display', 'list-style-type', 'vertical-align', 'white-space'},
}

_CLASSIC_FONTS = {
	'times': 'Times', 'times new roman': 'Times New Roman', 'georgia': 'Georgia', 'palatino': 'Palatino',
	'helvetica': 'Helvetica', 'arial': 'Arial', 'verdana': 'Verdana', 'geneva': 'Geneva', 'tahoma': 'Tahoma',
	'trebuchet ms': 'Trebuchet MS', 'courier': 'Courier', 'courier new': 'Courier New', 'monaco': 'Monaco',
	'chicago': 'Chicago', 'charcoal': 'Charcoal', 'new york': 'New York', 'lucida grande': 'Lucida Grande',
}
_GENERIC_FONTS = {
	'serif': ['Times', 'serif'], 'sans-serif': ['Helvetica', 'Arial', 'sans-serif'], 'monospace': ['Courier', 'monospace'],
	'system-ui': ['Geneva', 'Helvetica', 'sans-serif'], '-apple-system': ['Geneva', 'Helvetica', 'sans-serif'],
	'blinkmacsystemfont': ['Geneva', 'Helvetica', 'sans-serif'], 'ui-sans-serif': ['Helvetica', 'Arial', 'sans-serif'],
	'ui-serif': ['Times', 'serif'], 'ui-monospace': ['Monaco', 'Courier', 'monospace'],
}
_BAD_VALUE = re.compile(r'calc\(|clamp\(|min\(|max\(|var\(|env\(|attr\(|\d(?:vw|vh|vmin|vmax|ch|dvh|svh|lvh)\b|fit-content|max-content|min-content', re.I)
_REM = re.compile(r'(-?[\d.]+)rem\b')
_COLOR_TOKEN = re.compile(r'#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?|oklch|oklab|hwb)\([^)]*\)|\b[a-zA-Z]+\b')


def map_font_family(value):
	out = []
	for name in split_top(value, ','):
		key = name.strip().strip('"\'').lower()
		if key in _CLASSIC_FONTS:
			cand = [_CLASSIC_FONTS[key]]
		elif key in _GENERIC_FONTS:
			cand = _GENERIC_FONTS[key]
		else:
			continue
		for c in cand:
			if c not in out:
				out.append(c)
		if key in _GENERIC_FONTS:
			break
	return ', '.join('"%s"' % f if ' ' in f else f for f in out) or None


def downlevel_value(prop, value, variables):
	"""Rewrite a declaration value into something 1997-era CSS parsers handle, or None to drop it."""
	value = resolve_vars(value, variables)
	if _BAD_VALUE.search(value):
		return None
	value = _REM.sub(lambda m: '%gpx' % (float(m.group(1)) * 16), value)
	if prop in ('color', 'background-color', 'border-color'):
		return parse_color(value)
	if prop == 'font-family':
		return map_font_family(value)
	if prop == 'display':
		return value if value in ('none', 'block', 'inline', 'list-item') else None
	if prop.startswith('border'):
		value = _COLOR_TOKEN.sub(lambda m: parse_color(m.group(0)) or m.group(0), value)
	if prop == 'font-weight':
		return 'bold' if value in ('bold', 'bolder') or value.isdigit() and int(value) >= 600 else 'normal'
	if 'inherit' in value or 'initial' in value or 'unset' in value or 'revert' in value:
		return None
	return value


def downlevel_stylesheet(sheets, level, present=None, limit=24000):
	"""Emit a CSS1-ish stylesheet for `level` ('ns4' or 'ie').

	`present` is (tags, classes, ids) found in the document; rules that can't match it are dropped.
	Returns (css_text, used_classes)."""
	allowed = _PROPS.get(level)
	if not allowed:
		return '', set()
	variables = {}
	for sheet in sheets:
		variables.update(sheet.vars)
	tags, classes, ids = present if present else (None, None, None)
	used_classes = set()
	out = []
	size = 0
	for sheet in sheets:
		for rule in sheet.rules:
			sels = []
			for text in rule.selector_text:
				t = ' '.join(text.split())
				if _OUT_SEL.match(t):
					pass
				elif level == 'ie' and _OUT_SEL_LINK.match(t):
					pass
				else:
					continue
				words = re.findall(r'([.#]?)([_a-zA-Z][\w-]*)', t.split(':')[0])
				if present and not all(
						(name in classes) if kind == '.' else (name in ids) if kind == '#' else (name.lower() in tags)
						for kind, name in words):
					continue
				sels.append(t)
				used_classes.update(name for kind, name in words if kind == '.')
			if not sels:
				continue
			decls = []
			for prop, value, _ in rule.decls:
				if prop == 'background':
					color = next((c for c in (parse_color(tok) for tok in split_top(value, ' ')) if c), None)
					if color:
						decls.append('background-color: ' + color)
					continue
				if prop not in allowed:
					continue
				v = downlevel_value(prop, value, variables)
				if v:
					decls.append('%s: %s' % (prop, v))
			if not decls:
				continue
			line = '%s { %s }' % (', '.join(sels), '; '.join(decls))
			size += len(line) + 1
			if size > limit:
				return '\n'.join(out), used_classes
			out.append(line)
	return '\n'.join(out), used_classes


def downlevel_inline(style, level, variables):
	allowed = _PROPS.get(level)
	if not allowed or not style:
		return None
	out = []
	for prop, value, _ in parse_declarations(style):
		if prop in allowed:
			v = downlevel_value(prop, value, variables)
			if v:
				out.append('%s: %s' % (prop, v))
	return '; '.join(out) or None
