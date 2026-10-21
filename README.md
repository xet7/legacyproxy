The internet has changed a lot in the past few decades and has left the vintage computers that built it behind. LegacyProxy lets old browsers (NCSA Mosaic, Netscape 1.x–4.x, Internet Explorer 3–5, OmniWeb and friends) use the modern web: it fetches pages over modern TLS, rewrites the HTML (and the CSS) into something the browser understands, and shrinks images to a few kilobytes so they fit in a 1990s memory partition.

### Security

*Important:* this proxy strips all of the modern web's security between it and your old machine. Only run it on a private, trusted LAN. By default it refuses connections from anything but private and loopback addresses (see `--allow`).

### Installation

It is plain Python 3.9+ with no required packages.

**Raspberry Pi OS:** the system Python is fine. For image conversion install Pillow (often already present) or ImageMagick:

	sudo apt install python3-pil        # or: sudo apt install imagemagick
	python3 proxy.py

`legacyproxy.service` is a systemd unit for running it at boot.

**macOS:** Apple's `/usr/bin/python3` is linked against LibreSSL 2.8.3, which can't speak TLS 1.3, so many sites fail with it. Install a current Python once:

	brew install python                 # or use the python.org installer
	python3 proxy.py

If you start it with Apple's Python anyway, it finds the better Python and restarts itself under it. Images are converted with the built-in `sips` tool, so nothing else is needed.

### Setting up the browser

Every port the proxy listens on (default `8080`) speaks all of these, so use whichever your browser supports. Browsing to `http://<proxy-host>:8080/` shows a home page with these instructions and what the proxy detected about your browser.

* **HTTP proxy** (Netscape, IE, OmniWeb, Internet Config on Mac OS 8/9): set the HTTP proxy to the proxy host, port 8080. Leave the Security/SSL proxy empty and type `http://` even for secure sites. The proxy tries HTTPS first and remembers which sites need it.
* **Automatic proxy configuration** (Netscape 2+, IE 3+): use `http://<proxy-host>:8080/proxy.pac`. IE 5's auto-detect also finds `/wpad.dat` if you name the host `wpad` in DNS.
* **SOCKS 4, 4a or 5** (Internet Config, Netscape, IE): set the SOCKS host to the proxy host, port 8080. Web traffic to ports 80/8000/8080 gets rewritten like the HTTP proxy; everything else (FTP, news, mail…) passes through untouched.
* **No proxy at all** (NCSA Mosaic, anything else): put the proxy in front of an address, e.g. `http://<proxy-host>:8080/http://www.apple.com/`, or use the Go box on the home page (handy as your home page). Every link, image and form on the page stays inside the proxy. You can also point Mosaic's WAIS gateway at the proxy; then typing `wais://www.apple.com/` opens the site.

Typing an `https://` address in the browser won't work through an HTTP proxy, because old browsers then try to speak SSL 2/3 themselves and no modern site accepts that. Type `http://` instead. Links on proxied pages are rewritten for you.

### What it does to pages

The browser is identified from its User-Agent, and pages are written for what that browser can render:

* **Upstream:** sites always see a current desktop Chrome User-Agent (`--user-agent` to change it), modern TLS with certificate verification, and gzip. Redirects from `http` to `https` of the same address are followed silently.
* **HTML:** scripts, styles, SVG, video, `<template>` and friends are removed. HTML5 elements become `<div>`s (or line breaks for browsers without `<div>`), tables become lines for browsers without tables, JavaScript-only links and buttons are dropped, and `<button>`s become `<input type=submit>`. Lazy-loaded images are given their real `src`, the smallest suitable `srcset` image is chosen, and oversized `width`/`height` attributes are scaled down. Every URL is made absolute and points back through the proxy.
* **CSS:** stylesheets (including external and `@import`ed ones) are cascaded onto the page. Elements hidden by CSS (`display: none`, `visibility: hidden`, "screen reader only" tricks, media queries for other screen sizes) are removed, and colours, bold, italic, underline, alignment and backgrounds become `<font color>`, `<b>`, `align=`, `bgcolor=` and so on. Text colours that would be unreadable on the page's actual background are skipped. CSS variables, `oklch()`/`hsl()`/`rgb()` colours and CSS nesting are understood. Netscape 4 and IE 4/5 also get a small, sanitised CSS1 stylesheet containing only the rules the page uses.
* **Text:** everything is sent as ASCII with Latin-1 entities. Curly quotes, dashes, ellipses, arrows and so on are transliterated, accents outside Latin-1 are stripped, and emoji disappear. Form submissions are converted from Latin-1 to UTF-8.
* **Images:** everything (PNG, WebP, AVIF, SVG, huge JPEGs) becomes a baseline JPEG or GIF, at most 480 pixels and about 8 KB (`--image-max-size`, `--image-max-kb`). GIF is used for transparency and for Mosaic. The converter is Pillow if installed, otherwise `sips` (macOS), otherwise ImageMagick.
* **Cookies:** `Secure`/`SameSite` attributes are removed and expiry dates are rewritten in the format Netscape's original cookie spec used.

Run `python3 proxy.py --help` for all options, e.g. `--browser ie5` to force a profile or `-l 1080 -l 8080` to listen on several ports.

### Tests

	python3 -m unittest discover tests

### Limitations

* Sites that require JavaScript to render anything show little or nothing; the proxy says so.
* Some sites (news sites, search engines) block anything that isn't a real browser, regardless of User-Agent.
* `https://` typed directly into an old browser can't work (see above).
