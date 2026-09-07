# browser.py -- a minimal graphical web browser for Ripos (Phase 2).
#
# Fetches plain-HTTP pages with web.py, parses them with the stdlib
# html.parser, lays the content out into an offscreen page surface (text,
# headings, links, images) and renders a scrollable viewport on the
# framebuffer.  Keyboard-only: Tab cycles links, Enter follows, Backspace
# goes back, PgUp/PgDn scroll, u/Ctrl+L edits the address, R reloads,
# Esc quits.  Launched from the shell with:  browse <url>

import os
import kern
import appbar
import text as textmod
from keyboard import Keyboard
import tk
import web
import jpeg
import png
from html.parser import HTMLParser

BG = (14, 17, 24)
PANEL = (22, 27, 38)
FG = (205, 214, 228)
DIM = (125, 136, 156)
ACCENT = (96, 155, 255)
LINK = (120, 190, 255)
HEAD = (255, 230, 160)
ADDR_H = 24
STATUS_H = 15
TITLE_H = 16
MARGIN = 18
LINE_H = textmod.LINE_H + 4
MAX_PAGE_H = 4096


class _Parser(HTMLParser):
    """Turn HTML into a flat block list for the layout pass."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.title = ""
        self._in_title = False
        self._href = None
        self._text = []
        self._kind = "p"

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            self._flush()
            self._href = a.get("href")
        elif tag in ("h1", "h2", "h3"):
            self._flush()
            self._kind = "h" + tag[1]
        elif tag == "li":
            self._flush()
            self._kind = "li"
        elif tag == "br":
            self._flush()
        elif tag == "img":
            self._flush()
            self.blocks.append(("img", a.get("src"), a.get("alt")))
        elif tag in ("p", "pre"):
            self._flush()
            self._kind = "pre" if tag == "pre" else "p"

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._flush()
            self._href = None
        elif tag in ("p", "h1", "h2", "h3", "li", "pre"):
            self._flush()
            self._kind = "p"

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif data.strip() or self._text:
            self._text.append(data)

    def _flush(self):
        t = "".join(self._text).strip()
        self._text = []
        if not t:
            return
        if self._href is not None:
            self.blocks.append(("a", t, self._href))
        else:
            self.blocks.append((self._kind, t, None))


class _PageSurface(tk._ShadowFB):
    """Offscreen page buffer (text + images) the viewport scrolls over."""

    def __init__(self, w, h):
        self.width = w
        self.height = h
        self.stride = w
        self.bpp = 3
        self.format = 0
        self.mem = bytearray(w * h * 3)
        self.mirror_x = False
        self.mirror_glyphs = False
        self._ch = None
        self._phys_w = w
        self._phys_h = h

    def fill(self, rgb):
        pat = bytes(rgb)
        self.mem[0:] = pat * (self.width * self.height)

    def clear(self, r=0, g=0, b=0):
        self.fill((r, g, b))

    def blit_region(self, fb, ox, oy, sx, sy, w, h):
        bpp = self.bpp
        for yy in range(h):
            src = (sy + yy) * self.stride * bpp + sx * bpp
            dst = (oy + yy) * fb.stride * bpp + ox * bpp
            fb.mem[dst:dst + w * bpp] = self.mem[src:src + w * bpp]


class Browser(tk.Tk):
    """Full-screen browser: address bar on top, page viewport below."""

    def _cavity(self, fb):
        """Content area inside the app frame (sidebar + title bar)."""
        return (appbar.SIDEBAR_W + MARGIN, TITLE_H + MARGIN,
                fb.width - appbar.SIDEBAR_W - appbar.right_sidebar_w()
                - 2 * MARGIN,
                fb.height - TITLE_H - STATUS_H - 2 * MARGIN)

    def __init__(self, url, caller_kb, on_quit):
        tk.Tk.__init__(self, title="browser", sidebar=True)
        self.caller_kb = caller_kb
        self.on_quit = on_quit
        self.bg = BG
        self.show_title = True
        self.mode = "addr"
        self.addr = list(url)
        self.addr_cursor = len(url)
        self.history = []
        self._scroll = 0
        self._sel = 0
        self._page = None
        self._page_h = 0
        self._links = []
        self._title = ""
        self._status = "type a URL and press Enter (e.g. http://10.0.2.2:8000/)"
        self._load(url)

    # ---- fetch + layout ----------------------------------------------

    def _load(self, url):
        self._status = "fetching " + url
        self.redraw()
        try:
            status, headers, body = web.get(url)
            kern.write("browser: HTTP %d %d bytes\n" % (status, len(body)))
            if not (200 <= status < 300):
                self._status = "HTTP %d for %s" % (status, url)
                self.redraw()
                return
        except Exception as e:
            self._status = "error: %s" % (e,)
            kern.write("browser: fetch error: %s\n" % (e,))
            self.redraw()
            return
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = body.decode("latin-1", "replace")
        p = _Parser()
        try:
            p.feed(text)
        except Exception:
            pass
        self._title = (p.title or url).strip()[:60]
        kern.write("browser: %d blocks parsed\n" % len(p.blocks))
        self._build_page(p.blocks, url)
        self.history.append(url)
        self._scroll = 0
        self._sel = 0
        self._status = "loaded %s -- %d blocks" % (url, len(p.blocks))
        self.redraw()

    def _fetch_image(self, src, base_url):
        if not src or src.startswith("data:"):
            raise ValueError("no image url")
        if src.startswith("/"):
            host, port, _path = web._parse_url(base_url)
            url = "http://%s:%d%s" % (host, port, src)
        elif src.startswith("http://"):
            url = src
        else:
            raise ValueError("relative image: %r" % src)
        data = web.fetch(url)
        if data[:2] == b"\xff\xd8":
            return jpeg.decode_jpeg(data)
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return png.load_png(data)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            import gif as gifmod
            return gifmod.decode(data).frames[0][0]
        raise ValueError("unknown image type")

    def _build_page(self, blocks, base_url):
        vw = max(120, self.fb.width - appbar.SIDEBAR_W - appbar.right_sidebar_w() - 24)
        x0 = 10
        maxw = vw - 20
        ops = []
        links = []
        y = 8
        num = 1
        for kind, payload, extra in blocks:
            if y > MAX_PAGE_H - 200:
                break
            if kind == "img":
                try:
                    img = self._fetch_image(payload, base_url)
                except Exception:
                    continue
                scale = min(1.0, maxw / float(img.w)) if img.w else 1.0
                scale = max(0.05, scale)
                w = max(1, int(img.w * scale))
                h = max(1, int(img.h * scale))
                ops.append(("img", img, x0, y, scale))
                y += h + 8
                continue
            text = payload
            color = FG
            prefix = ""
            if kind == "a":
                color = LINK
                prefix = "[%d] " % num
            elif kind.startswith("h"):
                color = HEAD
            words = text.split()
            if not words:
                continue
            cur = ""
            first = True
            rect = None
            for wd in words:
                piece = (prefix + wd) if first else wd
                first = False
                if cur and textmod.text_width(cur + " " + piece) > maxw:
                    ops.append(("text", x0, y, cur, color))
                    if kind == "a" and rect is None:
                        rect = (num, text, extra, x0, y, maxw, LINE_H)
                    y += LINE_H
                    cur = piece
                else:
                    cur = cur + " " + piece if cur else piece
            if cur:
                ops.append(("text", x0, y, cur, color))
                if kind == "a" and rect is None:
                    rect = (num, text, extra, x0, y, maxw, LINE_H)
                y += LINE_H
            if kind == "a":
                if rect:
                    links.append(rect)
                num += 1
        page_h = min(MAX_PAGE_H, y + 8)
        page = _PageSurface(vw, page_h)
        page.fill(BG)
        for op in ops:
            if op[0] == "text":
                _, x, yy, s, color = op
                textmod.draw_text(page, x, yy, s, color)
            else:
                _, img, x, yy, scale = op
                try:
                    img.blit(page, x, yy, scale)
                except Exception:
                    pass
        self._page = page
        self._page_h = page_h
        self._links = links
        kern.write("browser: page %dx%d %d ops %d links\n"
                   % (vw, page_h, len(ops), len(links)))

    # ---- drawing ------------------------------------------------------

    def draw_region(self, fb, ox, oy, rw, rh):
        buffered, target = self._begin_shadow(fb, ox, oy, rw, rh)
        if target is None:
            return
        sx, sxr, y = self._draw_app_frame(target, ox, oy, rw, rh)
        cav_x, cav_y, cav_w, cav_h = self._cavity(fb)
        # address bar
        target.fill_rect(cav_x, cav_y, cav_w, ADDR_H, *PANEL)
        label = "> " + "".join(self.addr) + ("_" if self.mode == "addr" else "")
        textmod.draw_text(target, cav_x + 6, cav_y + 4, label, ACCENT)
        # page viewport
        py = cav_y + ADDR_H + 4
        ph = cav_h - ADDR_H - 4 - STATUS_H - 4
        target.fill_rect(cav_x, py, cav_w, ph, *BG)
        page = self._page
        if page is not None:
            vw = min(cav_w, page.width)
            vh = min(ph, page.height)
            try:
                page.blit_region(target, cav_x, py, 0, self._scroll, vw, vh)
            except Exception:
                pass
        # selected link highlight (inverse bar)
        if self._links and 0 <= self._sel < len(self._links):
            _num, _t, _h, lx, ly, lw, lh = self._links[self._sel]
            lyv = ly - self._scroll
            if py + 2 <= py + lyv < py + ph:
                target.fill_rect(cav_x + lx, py + lyv - 2, lw, lh, 40, 56, 84)
        # status bar
        sy = fb.height - STATUS_H
        target.fill_rect(sx, sy, fb.width - sx - sxr, STATUS_H, 10, 13, 18)
        textmod.draw_text(target, sx + 6, sy + 3, self._status, DIM)
        t = self._title
        if t and self._links:
            t += "  [Tab: %d links, Enter: follow, B: back, U: url, Esc: quit]" % len(self._links)
        textmod.draw_text(target, fb.width - sxr - 6 - textmod.text_width(t), sy + 3, t, DIM)
        self._end_shadow(fb, target, buffered)

    # ---- input --------------------------------------------------------

    def _scroll_links(self, d):
        if not self._links:
            return
        self._sel = (self._sel + d) % len(self._links)
        _num, _t, _h, _x, ly, _w, _h2 = self._links[self._sel]
        if ly < self._scroll:
            self._scroll = ly
        elif ly > self._scroll + self.fb.height - ADDR_H - 80:
            self._scroll = ly - (self.fb.height - ADDR_H - 120)
        self.redraw()

    def _follow(self):
        if not (0 <= self._sel < len(self._links)):
            return
        _num, _t, href, _x, _y, _w, _h = self._links[self._sel]
        if href:
            host, port, _p = web._parse_url(href) if href.startswith(("http://", "/")) else (None, None, None)
            url = href if href.startswith("http://") else "".join(self.addr)
            if href.startswith("/"):
                base = "".join(self.addr)
                bhost, bport, bpath = web._parse_url(base)
                url = "http://%s:%d%s" % (bhost, bport, href)
            self.addr = list(url)
            self._load(url)

    def on_key(self, ev):
        if self.destroyed:
            return
        name, ch, pressed = ev
        if not pressed:
            return
        if name == 'esc':
            self._quit()
            return
        if self.mode == "addr":
            if name == 'enter':
                url = "".join(self.addr).strip()
                if url and not url.startswith("http"):
                    url = "http://" + url
                self.mode = "page"
                self._load(url)
            elif name == 'backspace':
                if self.addr_cursor > 0:
                    del self.addr[self.addr_cursor - 1]
                    self.addr_cursor -= 1
                self.redraw()
            elif name == 'left':
                self.addr_cursor = max(0, self.addr_cursor - 1)
                self.redraw()
            elif name == 'right':
                self.addr_cursor = min(len(self.addr), self.addr_cursor + 1)
                self.redraw()
            elif ch is not None and 32 <= ch < 127:
                self.addr.insert(self.addr_cursor, chr(ch))
                self.addr_cursor += 1
                self.redraw()
            return
        # page mode
        if name == 'tab':
            self._scroll_links(1)
            return
        if name == 'enter' or ch in (ord('l'), ord('L')):
            self._follow() if name == 'enter' else None
            if ch in (ord('l'), ord('L')):
                self.mode = "addr"
                self.addr_cursor = len(self.addr)
                self.redraw()
            return
        if name == 'backspace' or ch in (ord('b'), ord('B')):
            if len(self.history) > 1:
                self.history.pop()
                prev = self.history[-1]
                self.addr = list(prev)
                self._load(prev)
            return
        if ch in (ord('r'), ord('R')) and self.history:
            url = self.history[-1]
            self._load(url)
            return
        if ch in (ord('u'),):
            self.mode = "addr"
            self.addr_cursor = len(self.addr)
            self.redraw()
            return
        if name == 'pagedown':
            self._scroll = min(max(0, self._page_h - self.fb.height + 120),
                               self._scroll + (self.fb.height - 160))
            self.redraw()
            return
        if name == 'pageup':
            self._scroll = max(0, self._scroll - (self.fb.height - 160))
            self.redraw()
            return
        if name == 'down':
            self._scroll = min(max(0, self._page_h - self.fb.height + 120),
                               self._scroll + LINE_H)
            self.redraw()
            return
        if name == 'up':
            self._scroll = max(0, self._scroll - LINE_H)
            self.redraw()
            return
        tk.Tk.on_key(self, ev)

    def _quit(self):
        if self.destroyed:
            return
        kern.write("browser: quit\n")
        try:
            self.caller_kb.activate()
        except AttributeError:
            kern.on_key(self.caller_kb._dispatch)
        self.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, url=None):
    """Start the browser.  kb is the CALLER keyboard (restored on quit)."""
    if url is None:
        url = "http://10.0.2.2:8000/"
    kern.write("browser: opening %s\n" % url)
    kb2 = Keyboard()
    root = Browser(url, kb, on_quit)
    root._prev_kb = kb
    root._back = on_quit
    root._close_handler = root._quit
    root.start(fb, kb2, mouse)
    kern.write("browser: screen ready\n")
    root.mainloop()
    kern.write("browser: ready (event-driven)\n")
