"""Shrink and convert images to small baseline JPEGs or GIFs.

Uses Pillow when it is installed, otherwise macOS `sips`, otherwise ImageMagick.
With none of them, GIF/JPEG pass through untouched and other formats are refused."""

import io
import os
import shutil
import struct
import subprocess
import tempfile
import threading

_slots = threading.BoundedSemaphore(2)  # image work is RAM-hungry on a Pi


def image_info(data):
	"""(format, width, height, progressive) from the header, or None."""
	try:
		if data[:6] in (b'GIF87a', b'GIF89a'):
			w, h = struct.unpack('<HH', data[6:10])
			return 'gif', w, h, False
		if data[:8] == b'\x89PNG\r\n\x1a\n':
			w, h = struct.unpack('>II', data[16:24])
			return 'png', w, h, False
		if data[:2] == b'\xff\xd8':
			i = 2
			while i + 9 < len(data):
				if data[i] != 0xff:
					i += 1
					continue
				marker = data[i + 1]
				if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
					i += 2
					continue
				length = struct.unpack('>H', data[i + 2:i + 4])[0]
				if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
					h, w = struct.unpack('>HH', data[i + 5:i + 9])
					return 'jpeg', w, h, marker in (0xc2, 0xc6, 0xca, 0xce)
				i += 2 + length
			return 'jpeg', 0, 0, False
		if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
			chunk = data[12:16]
			if chunk == b'VP8 ':
				w, h = struct.unpack('<HH', data[26:30])
				return 'webp', w & 0x3fff, h & 0x3fff, False
			if chunk == b'VP8L':
				b = data[21:25]
				w = 1 + (((b[1] & 0x3f) << 8) | b[0])
				h = 1 + (((b[3] & 0xf) << 10) | (b[2] << 2) | ((b[1] & 0xc0) >> 6))
				return 'webp', w, h, False
			if chunk == b'VP8X':
				w = 1 + int.from_bytes(data[24:27], 'little')
				h = 1 + int.from_bytes(data[27:30], 'little')
				return 'webp', w, h, False
			return 'webp', 0, 0, False
		if data[:2] == b'BM':
			w, h = struct.unpack('<ii', data[18:26])
			return 'bmp', w, abs(h), False
		head = data[:512].lstrip()
		if head[:5] == b'<?xml' or head[:4] == b'<svg' or b'<svg' in head:
			return 'svg', 0, 0, False
		if data[4:12] in (b'ftypavif', b'ftypavis', b'ftypheic', b'ftypmif1'):
			return 'avif', 0, 0, False
	except (struct.error, IndexError):
		pass
	return None


# ---------------------------------------------------------------- backends

class PillowBackend:
	name = 'Pillow'

	def __init__(self):
		from PIL import Image, ImageOps  # noqa: F401
		self.Image = Image
		self.ImageOps = ImageOps

	def load(self, data, max_side):
		img = self.Image.open(io.BytesIO(data))
		if img.format == 'JPEG':
			img.draft('RGB', (max_side, max_side))  # decode at reduced size: fast and frugal
		img.seek(0)
		img = self.ImageOps.exif_transpose(img)
		alpha = img.mode in ('RGBA', 'LA', 'PA') or (img.mode == 'P' and 'transparency' in img.info)
		img = img.convert('RGBA' if alpha else 'RGB')
		return {'img': img, 'alpha': alpha, 'size': img.size}

	def is_photo(self, h):
		small = h['img'].copy()
		small.thumbnail((64, 64))
		return small.convert('RGB').getcolors(256) is None

	def encode(self, h, fmt, w, hgt, quality, colors):
		img = h['img']
		if (w, hgt) != img.size:
			img = img.resize((w, hgt), self.Image.LANCZOS)
		out = io.BytesIO()
		if fmt == 'jpeg':
			if img.mode == 'RGBA':
				bg = self.Image.new('RGB', img.size, (255, 255, 255))
				bg.paste(img, mask=img.split()[3])
				img = bg
			img.save(out, 'JPEG', quality=quality, optimize=True, progressive=False)
		else:
			if img.mode == 'RGBA':
				mask = img.split()[3].point(lambda a: 255 if a < 128 else 0)
				pal = img.convert('RGB').quantize(colors=min(colors, 255))
				pal.paste(255, mask=mask)
				pal.save(out, 'GIF', transparency=255, optimize=True)
			else:
				img.quantize(colors=colors).save(out, 'GIF', optimize=True)
		return out.getvalue()

	def close(self, h):
		pass


class _CommandBackend:
	"""Shared temp-file handling for the command-line tools."""

	def load(self, data, max_side):
		fd, path = tempfile.mkstemp(prefix='legacyproxy-')
		with os.fdopen(fd, 'wb') as f:
			f.write(data)
		info = image_info(data)
		h = {'path': path, 'tmp': [path], 'fmt': info[0] if info else None}
		if h['fmt'] == 'svg':  # tools need the extension to recognise SVG
			os.rename(path, path + '.svg')
			h['path'] = path + '.svg'
			h['tmp'] = [h['path']]
		w, hgt, alpha = self.probe(h)
		if not w and info and info[1]:
			w, hgt = info[1], info[2]
		h['size'] = (w, hgt)
		h['alpha'] = alpha
		return h

	def is_photo(self, h):
		return h['fmt'] in ('jpeg', 'webp', 'avif', 'bmp')

	def _out(self, h, fmt):
		path = h['path'] + '.out.' + ('jpg' if fmt == 'jpeg' else 'gif')
		h['tmp'].append(path)
		return path

	def close(self, h):
		for p in h['tmp']:
			try:
				os.unlink(p)
			except OSError:
				pass


class SipsBackend(_CommandBackend):
	name = 'sips'

	def probe(self, h):
		r = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', '-g', 'hasAlpha', h['path']],
			stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
		vals = {}
		for line in r.stdout.decode('utf-8', 'replace').splitlines():
			if ':' in line:
				k, v = line.split(':', 1)
				vals[k.strip()] = v.strip()
		try:
			return int(vals.get('pixelWidth', 0)), int(vals.get('pixelHeight', 0)), vals.get('hasAlpha') == 'yes'
		except ValueError:
			return 0, 0, False

	def encode(self, h, fmt, w, hgt, quality, colors):
		out = self._out(h, fmt)
		cmd = ['sips', '-s', 'format', 'jpeg' if fmt == 'jpeg' else 'gif']
		if fmt == 'jpeg':
			cmd += ['-s', 'formatOptions', str(quality)]
		if (w, hgt) != h['size']:
			cmd += ['-z', str(hgt), str(w)]
		cmd += [h['path'], '--out', out]
		subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
		with open(out, 'rb') as f:
			return f.read()


class MagickBackend(_CommandBackend):
	name = 'ImageMagick'

	def __init__(self):
		magick = shutil.which('magick')
		self.convert = [magick] if magick else [shutil.which('convert')]
		self.identify = [magick, 'identify'] if magick else [shutil.which('identify')]

	def probe(self, h):
		r = subprocess.run(self.identify + ['-format', '%w %h %A', h['path'] + '[0]'],
			stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
		try:
			w, hgt, a = r.stdout.decode().split()[:3]
			return int(w), int(hgt), a.lower() in ('true', 'blend')
		except ValueError:
			return 0, 0, False

	def encode(self, h, fmt, w, hgt, quality, colors):
		out = self._out(h, fmt)
		cmd = self.convert + [h['path'] + '[0]', '-auto-orient']
		if (w, hgt) != h['size']:
			cmd += ['-thumbnail', '%dx%d!' % (w, hgt)]
		if fmt == 'jpeg':
			cmd += ['-background', 'white', '-flatten', '-strip', '-interlace', 'none', '-quality', str(quality), 'jpeg:' + out]
		else:
			cmd += ['-strip', '-colors', str(colors), 'gif:' + out]
		subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
		with open(out, 'rb') as f:
			return f.read()


class Backends:
	"""All usable converters, tried in order (Pillow can't read SVG; the command-line tools can)."""

	def __init__(self, backends):
		self.backends = backends
		self.name = ' + '.join(b.name for b in backends)

	def __bool__(self):
		return bool(self.backends)


def find_backends(preferred=None):
	found = []
	for name in [preferred] if preferred else ['pillow', 'sips', 'magick']:
		try:
			if name == 'pillow':
				found.append(PillowBackend())
			elif name == 'sips' and shutil.which('sips'):
				found.append(SipsBackend())
			elif name == 'magick' and (shutil.which('magick') or shutil.which('convert')):
				found.append(MagickBackend())
		except ImportError:
			continue
	return Backends(found) if found else None


# ---------------------------------------------------------------- policy

def _fit(w, h, max_side):
	if w <= max_side and h <= max_side:
		return w, h
	scale = max_side / float(max(w, h))
	return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def shrink(data, backends, profile, max_side=480, max_bytes=8192):
	"""Return (bytes, content_type) small enough for the old browser, or None if impossible."""
	info = image_info(data)
	fmt = info[0] if info else None
	if fmt in ('gif', 'jpeg') and not info[3] and len(data) <= max_bytes and info[1] <= max_side and info[2] <= max_side \
			and (fmt == 'gif' or profile.jpeg) and info[1]:
		return data, 'image/' + fmt
	for backend in backends.backends if backends else []:
		with _slots:
			try:
				h = backend.load(data, max_side)
			except Exception:
				continue
			try:
				result = _shrink_loaded(h, fmt, backend, profile, max_side, max_bytes)
			except Exception:
				result = None
			finally:
				backend.close(h)
		if result:
			return result
	if fmt in ('gif', 'jpeg') and (fmt == 'gif' or profile.jpeg):
		return data, 'image/' + fmt  # unconvertible but at least displayable
	return None


def _shrink_loaded(h, fmt, backend, profile, max_side, max_bytes):
	w0, h0 = h['size']
	if not w0 or not h0:
		return None
	if not profile.jpeg or h['alpha'] or fmt == 'gif':
		out_fmt = 'gif'
	elif fmt == 'jpeg':
		out_fmt = 'jpeg'
	else:
		out_fmt = 'jpeg' if backend.is_photo(h) else 'gif'
	w, hgt = _fit(w0, h0, max_side)
	quality, colors = 60, 256
	best = None
	for _ in range(6):
		out = backend.encode(h, out_fmt, w, hgt, quality, colors)
		if best is None or len(out) < len(best):
			best = out
		if len(out) <= max_bytes or max(w, hgt) <= 48:
			break
		ratio = max_bytes / float(len(out))
		scale = max(0.4, min(0.9, ratio ** 0.5 * 0.95))
		if out_fmt == 'jpeg':
			quality = max(30, quality - 10)
		else:
			colors = max(16, colors // 2)
		w, hgt = max(1, int(w * scale)), max(1, int(hgt * scale))
	return best, 'image/' + out_fmt
