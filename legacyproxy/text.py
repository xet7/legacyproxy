"""Character set handling: decode whatever the web sends, emit what 1995 can read."""

import codecs
import re
import unicodedata
from html.entities import codepoint2name
from urllib.parse import quote_plus, unquote_to_bytes

# characters outside Latin-1 that have a reasonable ASCII spelling
TRANSLIT = {
	'‘': "'", '’': "'", '‚': ',', '‛': "'", '′': "'",
	'“': '"', '”': '"', '„': '"', '‟': '"', '″': '"',
	'‹': '<', '›': '>',
	'‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '--', '―': '--', '−': '-',
	'…': '...', '•': '*', '‣': '>', '⁃': '-', '∙': '*', '●': '*', '◦': 'o',
	'™': '(TM)', '€': 'EUR', '№': 'No.',
	'←': '<-', '→': '->', '↑': '^', '↓': 'v', '↔': '<->', '⇒': '=>', '⇐': '<=',
	'▶': '>', '▸': '>', '►': '>', '◀': '<', '◂': '<', '▼': 'v', '▾': 'v', '▲': '^', '▴': '^',
	'★': '*', '☆': '*', '✓': 'v', '✔': 'v', '✕': 'x', '✖': 'x', '✗': 'x', '✘': 'x',
	'×': 'x', '≤': '<=', '≥': '>=', '≠': '!=', '≈': '~',
	' ': ' ', ' ': ' ', ' ': ' ', ' ': ' ', ' ': ' ', ' ': ' ', ' ': ' ',
	' ': ' ', ' ': ' ', ' ': ' ', ' ': ' ', '　': ' ',
	'​': '', '‌': '', '‍': '', '‎': '', '‏': '', '⁠': '', '﻿': '', '­': '',
	'️': '', '︎': '',
}

# Latin-1 symbols spelled out for browsers that predate their entities (Mosaic, Netscape 1.0)
LATIN1_ASCII = {
	'¡': '!', '¢': 'c', '£': 'GBP', '¤': '$', '¥': 'JPY', '¦': '|', '§': 'S.',
	'¨': '"', '©': '(c)', 'ª': 'a', '«': '<<', '¬': '-', '®': '(R)', '¯': '-',
	'°': ' deg', '±': '+/-', '²': '^2', '³': '^3', '´': "'", 'µ': 'u', '¶': 'P.',
	'·': '*', '¸': ',', '¹': '^1', 'º': 'o', '»': '>>', '¼': '1/4', '½': '1/2',
	'¾': '3/4', '¿': '?', '×': 'x', '÷': '/',
}

_CHARSET_META = re.compile(rb'''<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)''', re.I)
_CHARSET_CT = re.compile(r'charset\s*=\s*["\']?([\w.:-]+)', re.I)


def _codec(name):
	if not name:
		return None
	name = name.strip().lower()
	# browsers treat these as windows-1252, so do we
	if name in ('iso-8859-1', 'latin1', 'latin-1', 'us-ascii', 'ascii', 'iso8859-1'):
		name = 'cp1252'
	try:
		return codecs.lookup(name).name
	except LookupError:
		return None


def charset_from_content_type(content_type):
	m = _CHARSET_CT.search(content_type or '')
	return m.group(1) if m else None


def decode_body(body, content_type=None, sniff_html=True):
	"""Turn response bytes into text using header, BOM, <meta> and finally a utf-8/cp1252 guess."""
	if body.startswith(codecs.BOM_UTF8):
		return body[3:].decode('utf-8', 'replace')
	if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
		return body.decode('utf-16', 'replace')
	codec = _codec(charset_from_content_type(content_type))
	if not codec and sniff_html:
		m = _CHARSET_META.search(body[:4096])
		if m:
			codec = _codec(m.group(1).decode('ascii', 'ignore'))
	if codec:
		return body.decode(codec, 'replace')
	try:
		return body.decode('utf-8')
	except UnicodeDecodeError:
		return body.decode('cp1252', 'replace')


def _fallback(ch):
	"""Best-effort ASCII for a character with no entity or transliteration."""
	decomposed = unicodedata.normalize('NFKD', ch)
	kept = ''.join(c for c in decomposed if ord(c) < 128)
	if kept:
		return kept
	if unicodedata.category(ch)[0] in 'SCZM':  # symbols, emoji, controls, marks: drop
		return ''
	return '?'


def to_legacy_html(s, profile=None, attr=False):
	"""Escape text for HTML output using only ASCII and Latin-1 named entities."""
	if s.isascii():
		s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
		return s.replace('"', '&quot;') if attr else s
	nbsp = profile.nbsp if profile else True
	symbols = profile.latin1_symbols if profile else True
	out = []
	for ch in s:
		o = ord(ch)
		if o < 128:
			if ch == '&':
				out.append('&amp;')
			elif ch == '<':
				out.append('&lt;')
			elif ch == '>':
				out.append('&gt;')
			elif ch == '"' and attr:
				out.append('&quot;')
			else:
				out.append(ch)
			continue
		if o == 0xa0:
			out.append('&nbsp;' if nbsp else ' ')
			continue
		r = TRANSLIT.get(ch)
		if r is None and o < 256:
			if not symbols and ch in LATIN1_ASCII:
				r = LATIN1_ASCII[ch]
			elif o >= 0xa0:
				name = codepoint2name.get(o)
				out.append('&%s;' % name if name else '&#%d;' % o)
				continue
			else:
				r = ''  # C1 controls
		if r is None:
			r = _fallback(ch)
		out.append(r.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
	return ''.join(out)


def to_latin1(s):
	"""Plain text (no markup) as Latin-1 bytes, transliterating the rest."""
	out = []
	for ch in s:
		o = ord(ch)
		if o < 256:
			out.append(ch)
		else:
			r = TRANSLIT.get(ch)
			out.append(r if r is not None else _fallback(ch))
	return ''.join(out).encode('latin-1', 'replace')


def _transcode_piece(piece):
	if '%' not in piece and piece.isascii():
		return piece
	raw = unquote_to_bytes(piece.replace('+', ' '))
	try:
		raw.decode('utf-8')
		return piece  # already UTF-8 (or plain ASCII)
	except UnicodeDecodeError:
		return quote_plus(raw.decode('latin-1'), safe='')


def latin1_query_to_utf8(qs):
	"""Old browsers submit forms in Latin-1; modern servers expect UTF-8."""
	if not qs or ('%' not in qs and qs.isascii()):
		return qs
	parts = []
	for pair in qs.split('&'):
		if '=' in pair:
			k, v = pair.split('=', 1)
			parts.append(_transcode_piece(k) + '=' + _transcode_piece(v))
		else:
			parts.append(_transcode_piece(pair))
	return '&'.join(parts)
