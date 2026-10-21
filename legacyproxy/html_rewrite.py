"""Rewrite modern HTML into something Mosaic, Netscape 1-4, IE 3-5 and OmniWeb can render.

The page is parsed into a small tree, the stylesheets are cascaded onto it, and it is
re-serialised as HTML 3.2 (or less) with styles turned into <font>, <b>, align= and
bgcolor=, hidden elements removed, and every URL pointed back through the proxy."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

from . import css
from .text import to_legacy_html

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source',
	'track', 'wbr', 'keygen', 'frame', 'basefont', 'isindex', 'spacer'}

# dropped along with everything inside them
DROP = {'script', 'style', 'template', 'svg', 'math', 'canvas', 'video', 'audio', 'object', 'embed', 'applet',
	'noembed', 'source', 'track', 'param', 'head', 'title', 'meta', 'link', 'base', 'colgroup', 'col', 'wbr',
	'datalist', 'slot', 'portal', 'rp', 'rt', 'dialog', 'menuitem', 'search-results'}

# HTML5 block elements that old browsers ignore: become <div>
BLOCKS = {'header', 'footer', 'nav', 'main', 'article', 'section', 'aside', 'figure', 'figcaption', 'details',
	'hgroup', 'search', 'fieldset', 'legend', 'summary', 'div', 'address', 'center', 'form-group'}

# elements kept as-is (with whitelisted attributes)
KEEP = {'a', 'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'menu', 'dir',
	'blockquote', 'pre', 'listing', 'xmp', 'em', 'strong', 'b', 'i', 'u', 's', 'strike', 'tt', 'code', 'kbd', 'samp',
	'var', 'cite', 'dfn', 'big', 'small', 'sub', 'sup', 'font', 'basefont', 'nobr', 'table', 'tr', 'td', 'th',
	'caption', 'form', 'input', 'select', 'option', 'textarea', 'map', 'area', 'frameset', 'frame', 'noframes',
	'img', 'iframe', 'button', 'div', 'center', 'isindex'}

ATTRS = {
	'a': {'name'},
	'table': {'border', 'cellpadding', 'cellspacing', 'width', 'align', 'bgcolor'},
	'tr': {'align', 'valign', 'bgcolor'},
	'td': {'colspan', 'rowspan', 'align', 'valign', 'width', 'height', 'nowrap', 'bgcolor'},
	'th': {'colspan', 'rowspan', 'align', 'valign', 'width', 'height', 'nowrap', 'bgcolor'},
	'caption': {'align'},
	'div': {'align'}, 'p': {'align'}, 'h1': {'align'}, 'h2': {'align'}, 'h3': {'align'}, 'h4': {'align'},
	'h5': {'align'}, 'h6': {'align'},
	'hr': {'size', 'width', 'noshade', 'align'},
	'br': {'clear'},
	'ul': {'type', 'compact'}, 'ol': {'type', 'start', 'compact'}, 'li': {'type', 'value'}, 'dl': {'compact'},
	'font': {'size', 'color', 'face'}, 'basefont': {'size'},
	'form': {'method', 'enctype', 'name'},
	'input': {'type', 'name', 'value', 'size', 'maxlength', 'checked', 'align', 'alt'},
	'select': {'name', 'size', 'multiple'}, 'option': {'value', 'selected'},
	'textarea': {'name', 'rows', 'cols', 'wrap'},
	'button': {'type', 'name', 'value'},
	'map': {'name'}, 'area': {'shape', 'coords', 'alt', 'nohref'},
	'frameset': {'rows', 'cols', 'border', 'frameborder', 'framespacing'},
	'frame': {'name', 'scrolling', 'noresize', 'marginwidth', 'marginheight', 'frameborder'},
	'iframe': {'width', 'height', 'frameborder', 'scrolling', 'name'},
	'pre': {'width'},
	'isindex': {'prompt'},
}
URL_ATTRS = {'a': 'href', 'area': 'href', 'frame': 'src', 'iframe': 'src', 'input': 'src'}

BOLD_TAGS = {'b', 'strong', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'th', 'dt'}
ITALIC_TAGS = {'i', 'em', 'cite', 'var', 'dfn', 'address'}
ALIGNABLE = {'div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th'}
CONTENT_VOIDS = {'img', 'input', 'hr', 'select', 'textarea', 'iframe', 'isindex'}
TEXT_INPUTS = {'text', 'password', 'checkbox', 'radio', 'submit', 'reset', 'image', 'hidden', 'file'}

CLOSES_P = {'address', 'article', 'aside', 'blockquote', 'details', 'div', 'dl', 'fieldset', 'figcaption', 'figure',
	'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'main', 'menu', 'nav', 'ol', 'p', 'pre',
	'section', 'table', 'ul'}
SCOPE = {'table', 'td', 'th', 'button', 'html', 'body', '#document'}
MAX_DEPTH = 400


class Node:
	__slots__ = ('tag', 'attrs', 'children', 'parent', 'classes')

	def __init__(self, tag, attrs, parent):
		self.tag = tag
		self.attrs = attrs
		self.children = []
		self.parent = parent
		cls = attrs.get('class')
		self.classes = frozenset(cls.split()) if cls else frozenset()


class TreeBuilder(HTMLParser):
	"""Forgiving HTML -> Node tree. Head content is pulled out; everything renders under <body>."""

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.root = Node('#document', {}, None)
		self.html = Node('html', {}, self.root)
		self.body = Node('body', {}, self.html)
		self.root.children.append(self.html)
		self.html.children.append(self.body)
		self.stack = [self.root, self.html, self.body]
		self.styles = []     # ('inline', css_text, media) or ('link', href, media), in document order
		self.title = None
		self.base = None
		self.refresh = None
		self.fragments = set()
		self.tags, self.classes, self.ids = set(), set(), set()
		self._capture = None
		self._buf = []
		self._foreign = 0    # inside <svg>/<math>
		self._capture_media = ''

	def _attrs(self, attrs):
		d = {}
		for k, v in attrs:
			if k not in d:
				d[k] = '' if v is None else v
		return d

	def _close(self, names, stops):
		for i in range(len(self.stack) - 1, 2, -1):
			tag = self.stack[i].tag
			if tag in names:
				del self.stack[i:]
				return
			if tag in stops:
				return

	def handle_starttag(self, tag, attrs):
		if self._capture:
			return
		a = self._attrs(attrs)
		if tag in ('html', 'body'):
			node = self.html if tag == 'html' else self.body
			for k, v in a.items():
				node.attrs.setdefault(k, v)
			if 'class' in a:
				node.classes = frozenset(node.attrs['class'].split())
				self.classes.update(node.classes)
			if 'id' in a:
				self.ids.add(a['id'])
			return
		if tag == 'head':
			return
		if tag in ('svg', 'math'):
			self._foreign += 1
		if tag == 'title' and not self._foreign and self.title is None:
			self._capture, self._buf = 'title', []
			return
		if tag in ('script', 'style'):
			self._capture, self._buf = tag, []
			self._capture_media = a.get('media', '')
			return
		if tag == 'link':
			rel = a.get('rel', '').lower().split()
			if 'stylesheet' in rel and 'alternate' not in rel and a.get('href'):
				self.styles.append(('link', a['href'], a.get('media', '')))
			return
		if tag == 'meta':
			if a.get('http-equiv', '').lower() == 'refresh' and a.get('content'):
				self.refresh = a['content']
			return
		if tag == 'base':
			if a.get('href') and self.base is None:
				self.base = a['href']
			return
		self._open(tag, a)
		if tag in VOID:
			self.stack.pop()

	def handle_startendtag(self, tag, attrs):
		self.handle_starttag(tag, attrs)
		if tag not in VOID and not self._capture and self.stack[-1].tag == tag and len(self.stack) > 3:
			self.stack.pop()
		if tag in ('svg', 'math'):
			self._foreign -= 1

	def _open(self, tag, a):
		if tag in CLOSES_P:
			self._close({'p'}, SCOPE)
		if tag == 'li':
			self._close({'li'}, SCOPE | {'ul', 'ol', 'menu', 'dir'})
		elif tag in ('dt', 'dd'):
			self._close({'dt', 'dd'}, SCOPE | {'dl'})
		elif tag in ('td', 'th'):
			self._close({'td', 'th'}, {'tr', 'table'})
		elif tag == 'tr':
			self._close({'tr'}, {'table'})
		elif tag in ('thead', 'tbody', 'tfoot'):
			self._close({'thead', 'tbody', 'tfoot'}, {'table'})
		elif tag == 'option':
			self._close({'option'}, {'select', 'datalist'})
		elif tag == 'a':
			self._close({'a'}, SCOPE)
		parent = self.stack[-1]
		node = Node(tag, a, parent)
		parent.children.append(node)
		self.tags.add(tag)
		if node.classes:
			self.classes.update(node.classes)
		if 'id' in a:
			self.ids.add(a['id'])
		if tag in ('a', 'area') and a.get('href', '').startswith('#') and len(a['href']) > 1:
			self.fragments.add(a['href'][1:])
		if len(self.stack) < MAX_DEPTH:
			self.stack.append(node)
		else:
			self.stack.append(parent)  # flatten absurdly deep documents

	def handle_endtag(self, tag):
		if self._capture:
			if tag == self._capture:
				text = ''.join(self._buf)
				if tag == 'title':
					self.title = text
				elif tag == 'style':
					self.styles.append(('inline', text, self._capture_media))
				self._capture = None
			return
		if tag in ('html', 'head', 'body'):
			return
		if tag in ('svg', 'math'):
			self._foreign = max(0, self._foreign - 1)
		if tag == 'br':
			self._open('br', {})
			self.stack.pop()
			return
		for i in range(len(self.stack) - 1, 2, -1):
			if self.stack[i].tag == tag:
				del self.stack[i:]
				return
			if self.stack[i].tag in ('table',) and tag not in ('table',):
				return

	def handle_data(self, data):
		if self._capture:
			self._buf.append(data)
			return
		ch = self.stack[-1].children
		if ch and isinstance(ch[-1], str):
			ch[-1] += data
		else:
			ch.append(data)


class Ctx:
	"""Inherited state: c_* is what the CSS says, s_* is what the browser will actually show."""
	__slots__ = ('c_color', 'c_bold', 'c_italic', 'c_under', 'c_align',
		's_color', 's_bold', 's_italic', 's_under', 's_align', 's_bg', 'pre', 'link', 'form')

	def copy(self):
		n = Ctx()
		for k in self.__slots__:
			setattr(n, k, getattr(self, k))
		return n


_WS = re.compile(r'\s+')
_INT = re.compile(r'^\s*(\d+)(?:px)?\s*$')


def _int(v):
	m = _INT.match(v or '')
	return int(m.group(1)) if m else None


def _is_hidden(style):
	if not style:
		return False
	d = style.get('display', '').strip().lower()
	if d == 'none' or d.startswith('none '):
		return True
	if style.get('visibility', '').strip().lower() in ('hidden', 'collapse'):
		return True
	pos = style.get('position', '').strip().lower()
	if pos in ('absolute', 'fixed'):
		clip = style.get('clip', '').replace(' ', '')
		if clip.startswith('rect(0') or clip.startswith('rect(1px'):
			return True
		if 'inset(50%' in style.get('clip-path', '').replace(' ', ''):
			return True
		w, h = style.get('width', '').strip(), style.get('height', '').strip()
		if w in ('0', '0px', '1px') and h in ('0', '0px', '1px'):
			return True
		for side in ('left', 'top'):
			v = style.get(side, '').strip()
			if v.startswith('-') and (css._px(v) or 0) <= -999:
				return True
	return False


def _weight(style):
	w = style.get('font-weight')
	if w is None:
		f = style.get('font')
		if f:
			toks = f.lower().split()
			if 'bold' in toks or any(t.isdigit() and int(t) >= 600 for t in toks[:3]):
				return True
		return None
	w = w.strip().lower()
	if w in ('bold', 'bolder'):
		return True
	if w.isdigit():
		return int(w) >= 600
	return False if w in ('normal', 'lighter') else None


def _italic(style):
	s = style.get('font-style')
	if s is None:
		f = style.get('font', '')
		return True if 'italic' in f.lower().split()[:3] else None
	return s.strip().lower() in ('italic', 'oblique')


def _bg(style):
	c = style.get('background-color')
	if c is not None:
		return css.parse_color(c)
	b = style.get('background')
	if b:
		for tok in css.split_top(b, ' '):
			c = css.parse_color(tok)
			if c:
				return c
	return None


class Rewriter:
	def __init__(self, base_url, profile, map_url, max_image=480, viewport=800, stylesheets=None):
		self.base_url = base_url
		self.page_url = urldefrag(base_url)[0]
		self.profile = profile
		self.map_url = map_url        # absolute upstream URL -> URL the browser should use
		self.max_image = max_image
		self.viewport = viewport
		self.sheets = stylesheets or []
		self.engine = css.StyleEngine(self.sheets) if self.sheets else None
		self.used_classes = None
		self.out = []
		self.fragments = set()
		self.body_text = None
		self.link_colors = None

	# ------------------------------------------------------------ urls

	def url(self, raw):
		if raw is None:
			return None
		raw = ''.join(raw.split())  # browsers strip whitespace/newlines inside URLs
		if not raw:
			return None
		if raw.startswith('#'):
			return raw
		low = raw.lower()
		if low.startswith(('javascript:', 'data:', 'blob:', 'about:', 'vbscript:', 'tel:', 'sms:', 'intent:')):
			return None
		if low.startswith(('mailto:', 'news:', 'nntp:', 'ftp:', 'gopher:', 'telnet:', 'wais:')):
			return raw
		absolute = urljoin(self.base_url, raw)
		if not absolute.lower().startswith(('http://', 'https://')):
			return None
		doc, frag = urldefrag(absolute)
		if frag and doc == self.page_url:
			return '#' + frag
		return self.map_url(absolute)

	# ------------------------------------------------------------ output helpers

	def text(self, s, ctx):
		if not ctx.pre:
			s = _WS.sub(' ', s)
		return to_legacy_html(s, self.profile)

	def attr(self, v):
		return to_legacy_html(v, self.profile, attr=True)

	def open_tag(self, tag, attrs):
		parts = [tag]
		for k, v in attrs:
			if v is None:
				parts.append(k)
			else:
				parts.append('%s="%s"' % (k, self.attr(str(v))))
		return '<%s>' % ' '.join(parts)

	def style_of(self, node):
		return self.engine.computed(node) if self.engine else {}

	# ------------------------------------------------------------ document

	def rewrite(self, builder):
		p = self.profile
		self.fragments = builder.fragments
		if builder.base:
			self.base_url = urljoin(self.base_url, builder.base)

		html_style = self.style_of(builder.html)
		body_style = self.style_of(builder.body)
		bg = _bg(body_style) or _bg(html_style)
		color = css.parse_color(body_style.get('color') or html_style.get('color') or '')
		body_attrs = []
		if p.body_colors:
			bg = bg or css.parse_color(builder.body.attrs.get('bgcolor', '')) or '#ffffff'
			body_attrs.append(('bgcolor', bg))
			if color and css.contrast(color, bg) >= 3:
				body_attrs.append(('text', color))
			else:
				color = None
			if css.luminance(bg) < 0.2:
				color = color or '#ffffff'
				if ('text', color) not in body_attrs:
					body_attrs.append(('text', color))
				body_attrs += [('link', '#99bbff'), ('vlink', '#cc99ff')]
			bgimg = builder.body.attrs.get('background')
			if bgimg and self.url(bgimg):
				body_attrs.append(('background', self.url(bgimg)))
		else:
			bg, color = None, None
		self.body_text = color

		ctx = Ctx()
		ctx.c_color = css.parse_color(body_style.get('color') or html_style.get('color') or '') or color
		ctx.c_bold = bool(_weight(body_style))
		ctx.c_italic = bool(_italic(body_style))
		ctx.c_under = False
		ctx.c_align = (body_style.get('text-align') or '').strip().lower() or 'left'
		ctx.s_color, ctx.s_bold, ctx.s_italic, ctx.s_under, ctx.s_align = color, False, False, False, 'left'
		ctx.s_bg = bg or '#ffffff'
		ctx.pre = ctx.link = ctx.form = False

		style_block = ''
		if p.css and self.sheets:
			# HTML5 elements are renamed on output, so selectors naming them can't match any more
			present = ((builder.tags & KEEP) | {'body', 'html', 'a'}, builder.classes, builder.ids)
			sheet_text, self.used_classes = css.downlevel_stylesheet(self.sheets, p.css, present)
			if sheet_text:
				style_block = '<style type="text/css"><!--\n%s\n--></style>\n' % sheet_text

		body_out = []
		self.out = body_out
		produced = self.children(builder.body, ctx)
		if not produced:
			body_out.append('<p>(This page shows nothing without JavaScript or images.)</p>')

		title = _WS.sub(' ', builder.title or '').strip() or self.page_url
		head = ['<html>\n<head>\n<title>', to_legacy_html(title, p), '</title>\n']
		if builder.refresh:
			head.append(self._refresh(builder.refresh))
		head.append(style_block)
		head.append('</head>\n')
		body_tag = self.open_tag('body', body_attrs)
		result = ''.join(head) + body_tag + '\n' + ''.join(body_out) + '\n</body>\n</html>\n'
		if not p.div:
			result = re.sub(r'(?:<br>\s*){3,}', '<br><br>\n', result)
		return result

	def _refresh(self, content):
		m = re.match(r'\s*(\d+)\s*(?:[;,]\s*(?:url\s*=\s*)?["\']?([^"\']*)["\']?)?', content, re.I)
		if not m:
			return ''
		delay, target = m.groups()
		if target:
			target = self.url(target)
			if not target:
				return ''
			return '<meta http-equiv="refresh" content="%s; url=%s">\n' % (delay, self.attr(target))
		return '<meta http-equiv="refresh" content="%s">\n' % delay

	# ------------------------------------------------------------ tree walk

	def children(self, node, ctx):
		produced = False
		for child in node.children:
			if isinstance(child, str):
				if not child:
					continue
				t = self.text(child, ctx)
				if t.strip():
					produced = True
				elif self.out and self.out[-1].endswith(' '):
					continue
				self.out.append(t)
			elif self.element(child, ctx):
				produced = True
		return produced

	def element(self, node, ctx):
		tag = node.tag
		a = node.attrs
		p = self.profile
		if tag in DROP and not (tag == 'dialog' and 'open' in a):
			return False
		if tag == 'iframe' and not p.iframe:
			return False
		if 'hidden' in a and not (tag == 'input' and a.get('type', '').lower() == 'hidden'):
			return False
		if a.get('aria-hidden') == 'true' and tag in ('i', 'span') and not node.children:
			return False
		style = self.style_of(node) if tag not in ('br', 'option') else {}
		if _is_hidden(style) and not (tag == 'input' and a.get('type', '').lower() == 'hidden'):
			return False

		if tag == 'noscript' or tag == 'picture':
			return self.children(node, ctx)
		if tag == 'img':
			return self.img(node, ctx)
		if tag == 'input':
			return self.input(node, ctx)
		if tag == 'button':
			return self.button(node, ctx)
		if tag == 'q':
			self.out.append('"')
			r = self.children(node, ctx)
			self.out.append('"')
			return r
		if tag == 'mark':
			tag = 'b'
		if tag in ('thead', 'tbody', 'tfoot', 'span', 'label', 'time', 'data', 'abbr', 'acronym', 'ins', 'output',
				'bdi', 'bdo', 'ruby'):
			return self.inline_wrapper(node, ctx, style)
		if tag == 'del':
			tag = 's'
		if tag == 'dialog':
			tag = 'div'
		if tag in BLOCKS:
			tag = 'div' if tag != 'center' else 'center'
			if tag == 'center' and not p.center:
				tag = 'div'
			if tag == 'div' and not p.div:
				return self.block_fallback(node, ctx, style)
		elif tag not in KEEP:
			return self.inline_wrapper(node, ctx, style)  # custom elements and other unknowns
		if tag in ('table', 'tr', 'td', 'th', 'caption') and not p.tables:
			return self.table_fallback(node, ctx, style)
		return self.generic(node, tag, ctx, style)

	def _anchor(self, node):
		ident = node.attrs.get('id') or (node.attrs.get('name') if node.tag != 'a' else None)
		if ident and ident in self.fragments:
			self.out.append('<a name="%s"></a>' % self.attr(ident))
			return True
		return False

	def _css_attrs(self, node, out_attrs):
		"""Keep class/id/style for browsers that get a downlevelled stylesheet."""
		level = self.profile.css
		if not level:
			return
		if 'id' in node.attrs:
			out_attrs.append(('id', node.attrs['id']))
		if node.classes and self.used_classes:
			keep = [c for c in node.attrs['class'].split() if c in self.used_classes]
			if keep:
				out_attrs.append(('class', ' '.join(keep)))
		if node.attrs.get('style'):
			s = css.downlevel_inline(node.attrs['style'], level, self.engine.vars if self.engine else {})
			if s:
				out_attrs.append(('style', s))

	def _apply_style(self, node, tag, style, ctx, out_attrs):
		"""Work out wrappers (<font>, <b>, ...) and attributes from computed style. Returns (opens, closes, ctx)."""
		p = self.profile
		n = ctx.copy()
		if tag in ('td', 'th'):
			# old browsers reset fonts inside table cells
			n.s_color = self.body_text
			n.s_bold = tag == 'th'
			n.s_italic = n.s_under = False
			n.s_align = 'center' if tag == 'th' else 'left'
		if tag in BOLD_TAGS:
			n.c_bold = n.s_bold = True
		if tag in ITALIC_TAGS:
			n.c_italic = n.s_italic = True
		if tag == 'a' and 'href' in node.attrs:
			n.link = True
		if tag in ('pre', 'listing', 'xmp', 'textarea'):
			n.pre = True
		opens, closes = [], []
		if not style:
			return opens, closes, n

		cv = style.get('color')
		if cv is not None:
			c = css.parse_color(cv)
			if c:
				n.c_color = c
		w = _weight(style)
		if w is not None:
			n.c_bold = w
		it = _italic(style)
		if it is not None:
			n.c_italic = it
		td = (style.get('text-decoration-line') or style.get('text-decoration') or '').lower()
		if td:
			n.c_under = 'underline' in td
		al = (style.get('text-align') or '').strip().lower()
		if al in ('left', 'right', 'center', 'start', 'end', 'justify'):
			n.c_align = {'start': 'left', 'end': 'right', 'justify': 'left'}.get(al, al)

		bg = _bg(style)
		if bg and tag in ('table', 'tr', 'td', 'th') and p.cell_bgcolor and 'bgcolor' not in node.attrs:
			out_attrs.append(('bgcolor', bg))
			n.s_bg = bg

		if n.c_align != n.s_align and n.c_align in ('center', 'right'):
			if tag in ALIGNABLE and (tag != 'div' or p.div):
				out_attrs.append(('align', n.c_align))
				n.s_align = n.c_align
		elif n.c_align == 'left' and n.s_align != 'left' and tag in ALIGNABLE and tag != 'td':
			out_attrs.append(('align', 'left'))
			n.s_align = 'left'

		if (p.font_color and not n.link and n.c_color and n.c_color != n.s_color
				and css.contrast(n.c_color, n.s_bg) >= 3):
			opens.append('<font color="%s">' % n.c_color)
			closes.insert(0, '</font>')
			n.s_color = n.c_color
		if n.c_bold and not n.s_bold:
			opens.append('<b>')
			closes.insert(0, '</b>')
			n.s_bold = True
		if n.c_italic and not n.s_italic:
			opens.append('<i>')
			closes.insert(0, '</i>')
			n.s_italic = True
		if n.c_under and not n.s_under and not n.link:
			opens.append('<u>')
			closes.insert(0, '</u>')
			n.s_under = True
		return opens, closes, n

	def generic(self, node, tag, ctx, style):
		a = node.attrs
		p = self.profile
		out_attrs = []
		allowed = ATTRS.get(tag, ())
		for k in allowed:
			if k in a:
				out_attrs.append((k, a[k] if a[k] != '' or k in ('value', 'alt') else None))
		url_attr = URL_ATTRS.get(tag)
		link_target = None
		if url_attr and url_attr in a:
			link_target = self.url(a[url_attr])
			if link_target:
				out_attrs.insert(0, (url_attr, link_target))
		if tag == 'form':
			action = self.url(a.get('action') or self.base_url) or self.map_url(self.page_url)
			out_attrs.insert(0, ('action', action))
			method = a.get('method', 'get').lower()
			out_attrs = [x for x in out_attrs if x[0] != 'method']
			if method == 'post':
				out_attrs.append(('method', 'post'))
		if tag in ('table', 'td', 'th'):
			w = _int(a.get('width'))
			if w and w > self.viewport:
				out_attrs = [x for x in out_attrs if x[0] != 'width']
				if tag == 'table':
					out_attrs.append(('width', '100%'))
		if tag in ('td', 'th', 'table', 'body') and a.get('background') and p.cell_bgcolor:
			bgurl = self.url(a['background'])
			if bgurl:
				out_attrs.append(('background', bgurl))
		self._css_attrs(node, out_attrs)

		opens, closes, n = self._apply_style(node, tag, style, ctx, out_attrs)
		if tag == 'form':
			n.form = True

		anchored = self._anchor(node)
		if tag in VOID:
			self.out.append(self.open_tag(tag, out_attrs))
			return tag in CONTENT_VOIDS or anchored
		if tag == 'div' and not out_attrs and not opens and _only_block_child(node):
			return self.children(node, n) or anchored  # <div><div>x</div></div> is just <div>x</div>
		mark = len(self.out)
		self.out.append(self.open_tag(tag, out_attrs))
		self.out.extend(opens)
		inner = len(self.out)
		produced = self.children(node, n)
		if tag == 'a' and link_target and not produced:
			label = a.get('aria-label') or a.get('title')
			if label:
				self.out.append(self.text(label, n))
				produced = True
		if not produced and tag not in ('td', 'th', 'tr', 'textarea', 'frameset', 'frame', 'noframes', 'iframe') \
				and not (tag == 'a' and 'name' in a):
			del self.out[mark:]
			return anchored
		if tag == 'a' and not link_target and 'name' not in a:
			# javascript: or unusable link: keep the text, drop the link
			kept = self.out[inner:]
			del self.out[mark:]
			self.out.extend(kept)
			return produced
		self.out.extend(closes)
		self.out.append('</%s>' % tag)
		if tag in ('ul', 'ol', 'dl', 'table') or tag.startswith('h') and len(tag) == 2:
			self.out.append('\n')
		return True

	def inline_wrapper(self, node, ctx, style):
		"""Elements without an HTML 3.2 equivalent: render the children, keep visual style."""
		out_attrs = []
		opens, closes, n = self._apply_style(node, node.tag, style, ctx, out_attrs)
		anchored = self._anchor(node)
		mark = len(self.out)
		self.out.extend(opens)
		produced = self.children(node, n)
		if not produced:
			del self.out[mark:]
			return anchored
		self.out.extend(closes)
		disp = (style.get('display') or '').strip().lower()
		if disp in ('block', 'flex', 'grid', 'list-item', 'table') and not (self.out and self.out[-1] == '<br>'):
			self.out.append('<br>')
		return True

	def block_fallback(self, node, ctx, style):
		"""<div> for browsers that predate it: children followed by a line break."""
		out_attrs = []
		opens, closes, n = self._apply_style(node, 'div', style, ctx, out_attrs)
		center = self.profile.center and n.c_align == 'center' and ctx.s_align != 'center'
		anchored = self._anchor(node)
		mark = len(self.out)
		if center:
			self.out.append('<center>')
			n.s_align = 'center'
		self.out.extend(opens)
		produced = self.children(node, n)
		if not produced:
			del self.out[mark:]
			return anchored
		self.out.extend(closes)
		self.out.append('</center>\n' if center else '<br>\n')
		return True

	def table_fallback(self, node, ctx, style):
		"""Tables for browsers without them: rows become lines, cells are separated by spaces."""
		tag = node.tag
		mark = len(self.out)
		if tag == 'th':
			self.out.append('<b>')
		produced = self.children(node, ctx)
		if not produced:
			del self.out[mark:]
			return False
		if tag == 'th':
			self.out.append('</b>')
		if tag in ('td', 'th'):
			self.out.append(' &nbsp; ' if self.profile.nbsp else ' | ')
		elif tag in ('tr', 'caption'):
			self.out.append('<br>\n')
		elif tag == 'table':
			self.out.append('<p>\n')
		return True

	# ------------------------------------------------------------ special elements

	def pick_image(self, a):
		src = a.get('src', '').strip()
		lazy = None
		for k in ('data-src', 'data-lazy-src', 'data-original', 'data-lazy', 'data-url', 'data-hi-res-src'):
			if a.get(k, '').strip() and not a[k].strip().startswith('data:'):
				lazy = a[k].strip()
				break
		srcset = a.get('srcset') or a.get('data-srcset') or a.get('data-lazy-srcset')
		best = None
		if srcset:
			cands = []
			for item in re.split(r',\s+(?=\S)', srcset.strip()):
				bits = item.split()
				if not bits or bits[0].startswith('data:'):
					continue
				size = 1
				if len(bits) > 1:
					d = bits[1]
					try:
						size = float(d[:-1]) * (1 if d.endswith('w') else 10000) if d[-1] in 'wx' else 1
					except ValueError:
						size = 1
				cands.append((size, bits[0]))
			if cands:
				cands.sort()
				big_enough = [c for c in cands if c[0] >= self.max_image and c[0] < 10000]
				best = (big_enough[0] if big_enough else cands[0] if cands[-1][0] >= 10000 else cands[-1])[1]
		if src and not src.startswith('data:') and not lazy:
			return best or src
		return lazy or best or (src if src and not src.startswith('data:') else None)

	def img(self, node, ctx):
		a = node.attrs
		src = self.pick_image(a)
		target = self.url(src) if src else None
		alt = _WS.sub(' ', a.get('alt', '')).strip()[:120]
		if not target:
			return False
		w, h = _int(a.get('width')), _int(a.get('height'))
		if w is not None and h is not None and w <= 2 and h <= 2:
			return False  # tracking pixel
		attrs = [('src', target), ('alt', alt)]
		if w and h:
			if max(w, h) > self.max_image:
				scale = self.max_image / float(max(w, h))
				w, h = max(1, int(w * scale)), max(1, int(h * scale))
			attrs += [('width', w), ('height', h)]
		elif w and w > self.max_image:
			pass  # unknown aspect ratio: let the browser use the real (already shrunk) size
		elif w and not h:
			attrs.append(('width', w))
		for k in ('align', 'hspace', 'vspace', 'usemap', 'ismap'):
			if k in a and a[k] is not None:
				attrs.append((k, a[k] or None))
		if ctx.link:
			attrs.append(('border', '0'))
		self._anchor(node)
		self.out.append(self.open_tag('img', attrs))
		return True

	def input(self, node, ctx):
		a = node.attrs
		t = a.get('type', 'text').lower()
		if t == 'button' or t in ('submit', 'reset', 'image') and not ctx.form:
			return False  # only scripts can make these do anything
		if t not in TEXT_INPUTS:
			t = 'text'
		attrs = [('type', t)]
		for k in ('name', 'value', 'size', 'maxlength', 'alt'):
			if k in a:
				attrs.append((k, a[k]))
		if t == 'text' and 'size' not in a:
			attrs.append(('size', '30'))
		if 'checked' in a:
			attrs.append(('checked', None))
		if t == 'image':
			src = self.url(a.get('src'))
			if not src:
				attrs[0] = ('type', 'submit')
			else:
				attrs.append(('src', src))
		if t in ('submit', 'reset') and not a.get('value'):
			label = a.get('aria-label') or a.get('title')
			if label:
				attrs.append(('value', label))
		self.out.append(self.open_tag('input', attrs))
		return True

	def button(self, node, ctx):
		a = node.attrs
		t = a.get('type', 'submit').lower()
		if t not in ('submit', 'reset') or not ctx.form and not a.get('form'):
			return False  # type=button, or outside a form: only scripts can make it do anything
		label = _WS.sub(' ', _text_content(node)).strip() or a.get('aria-label') or a.get('title') or 'Submit'
		if self.profile.button:
			attrs = [('type', t)]
			for k in ('name', 'value'):
				if k in a:
					attrs.append((k, a[k]))
			self.out.append(self.open_tag('button', attrs) + self.text(label, ctx) + '</button>')
			return True
		attrs = [('type', t), ('value', label)]
		if a.get('name') and t == 'submit':
			attrs.append(('name', a['name']))
		self.out.append(self.open_tag('input', attrs))
		return True


_BLOCK_OUT = BLOCKS | {'p', 'table', 'ul', 'ol', 'dl', 'pre', 'blockquote', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


def _only_block_child(node):
	"""True if the node holds exactly one element, which renders as a block anyway."""
	child = None
	for c in node.children:
		if isinstance(c, str):
			if c.strip():
				return False
		elif c.tag not in DROP:
			if child is not None:
				return False
			child = c
	return child is not None and child.tag in _BLOCK_OUT


def _text_content(node):
	out = []
	stack = [node]
	while stack:
		n = stack.pop()
		if isinstance(n, str):
			out.append(n)
		elif n.tag not in DROP:
			stack.extend(reversed(n.children))
	return ''.join(out)


def parse(html_text):
	b = TreeBuilder()
	try:
		b.feed(html_text)
		b.close()
	except Exception:  # html.parser is forgiving, but keep whatever we got
		pass
	return b


def rewrite_html(html_text, base_url, profile, map_url, fetch_css=None, max_image=480, viewport=800):
	"""Rewrite a page. `fetch_css(url) -> text or None` loads external stylesheets."""
	builder = parse(html_text)
	sheets = load_stylesheets(builder.styles, base_url if not builder.base else urljoin(base_url, builder.base),
		fetch_css, viewport)
	return Rewriter(base_url, profile, map_url, max_image, viewport, sheets).rewrite(builder)


def load_stylesheets(refs, base_url, fetch_css, viewport):
	"""Fetch and parse the page's stylesheets (in order, with @imports, in parallel)."""
	from concurrent.futures import ThreadPoolExecutor
	sources = []
	for ref in refs:
		kind, value, media = ref
		if media and not css.media_matches(media, viewport):
			continue
		if kind == 'inline':
			sources.append(('text', value, base_url))
		elif fetch_css:
			sources.append(('url', urljoin(base_url, value), None))
	urls = [s[1] for s in sources if s[0] == 'url']
	texts = {}
	if urls and fetch_css:
		with ThreadPoolExecutor(max_workers=4) as pool:
			for u, t in zip(urls, pool.map(fetch_css, urls[:12])):
				texts[u] = t
	sheets = []
	for kind, value, base in sources:
		if kind == 'text':
			text, base = value, base
		else:
			text, base = texts.get(value), value
		if not text:
			continue
		_add_sheet(sheets, text, base, fetch_css, viewport, 0)
	return sheets


def _add_sheet(sheets, text, base, fetch_css, viewport, depth):
	sheet = css.parse_stylesheet(text, viewport)
	if fetch_css and depth < 2:
		for url, media in sheet.imports:
			if media and not css.media_matches(media, viewport):
				continue
			absolute = urljoin(base, url)
			imported = fetch_css(absolute)
			if imported:
				_add_sheet(sheets, imported, absolute, fetch_css, viewport, depth + 1)
	sheets.append(sheet)
