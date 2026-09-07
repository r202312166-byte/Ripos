# imgview.py -- image viewer for Ripos (M9.2).
#
# Pure-Python viewer for PNG (png.py) and baseline JPEG (jpeg.py) images:
# the image is scaled to fit the screen and centered, with real mouse
# support.  Controls:
#   Esc (or q)            quits back to the caller (shell / file manager)
#   +/-                   zoom in / out
#   mouse wheel           zoom in / out around the cursor
#   drag with left button pan (when the image is larger than the screen)
#   arrow keys            pan 24 px
#   Home (or r button)    refit (and re-center)
# Transparent PNG areas are alpha-composited over a checkerboard so the
# colours show correctly.  The kernel only supplies the framebuffer + keys
# -- the viewer is Python.
#
# Launch from the M8 shell with:  import imgview      (demo image)
#                                 imgview.view('/home/x.png')
# or from the file manager by double-clicking a .png/.jpg/.jpeg.

import kern
import png
import jpeg
import appbar
import text as textmod
from keyboard import Keyboard
import tkinter as tk

TITLE_BG = (70, 95, 140)
TITLE_TEXT = (255, 255, 255)
TITLE_H = 16
STATUS_H = 15
BG = (16, 20, 28)
MARGIN = 18

# Zoom steps.  Only 1/1, 1/k down and integer up are used, so every zoom
# repaint takes the C-speed slice blit path (no per-pixel Python fallback).
ZOOM_STEPS = (1.0 / 16, 1.0 / 8, 1.0 / 6, 1.0 / 5, 1.0 / 4, 1.0 / 3,
              1.0 / 2, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0, 16.0)


def load_image(path):
    """Load a PNG or baseline-JPEG file into an opaque RGB png.Image:
    RGBA PNGs are alpha-composited over a checkerboard so transparent areas
    display correctly instead of as garbage."""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        img = png.load_png(data)
    elif data[:2] == b"\xff\xd8":
        img = jpeg.decode_jpeg(data)
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        # static view: the first frame of an animated GIF (M9.7)
        import gif as gifmod
        g = gifmod.decode(data)
        img = g.frames[0][0]
        if len(g.frames) > 1:
            kern.write("imgview: GIF has %d frames; showing frame 1\n"
                       % len(g.frames))
    else:
        raise ValueError("imgview: unsupported image format: %s" % path)
    if img.mode == "rgba":
        kern.write("imgview: flattening %dx%d RGBA\n" % (img.w, img.h))
        img = img.composite(checker=((52, 58, 72), (30, 34, 44), 8))
    return img


def demo_image():
    """A built-in gradient demo image (no file needed)."""
    w, h = 192, 108
    img = png.Image(w, h, "rgb")
    px = img.pixels
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * 3
            px[o] = x * 255 // w
            px[o + 1] = y * 255 // h
            px[o + 2] = 255 - x * 255 // w
    return img


class Viewer(tk.Tk):
    """A full-screen image viewer: a Tk root whose draw_region blits the
    image (scaled to fit, centered) instead of painting widgets."""

    def __init__(self, img, path, caller_kb, on_quit):
        tk.Tk.__init__(self, title="imgview: %s" % path, sidebar=True)
        self.img = img
        self.caller_kb = caller_kb
        self.on_quit = on_quit
        self.bg = BG
        self.show_title = True
        self.scale = 1.0
        self.ox = 0
        self.oy = 0
        self._drag0 = None
        self._mouse_any = self._on_mouse_any

    def start(self, fb, kb, mouse=None):
        tk.Tk.start(self, fb, kb, mouse)
        self._fit()
        self.redraw()

    def _cavity(self, fb):
        """(x0, y0, w, h) of the area available to the image."""
        return (MARGIN, TITLE_H + MARGIN,
                fb.width - appbar.SIDEBAR_W - appbar.right_sidebar_w() - 2 * MARGIN,
                fb.height - TITLE_H - STATUS_H - 2 * MARGIN)

    def _snap(self, s):
        best = ZOOM_STEPS[0]
        for v in ZOOM_STEPS:
            if abs(v - s) < abs(best - s):
                best = v
        return best

    def _fit(self):
        fb = self.fb
        if fb is None:
            return
        img = self.img
        cw, ch = self._cavity(fb)[2], self._cavity(fb)[3]
        fit = min(cw / img.w, ch / img.h)
        self.scale = max(0.0625, self._snap(min(16.0, fit)))
        self._center()

    def _center(self):
        fb = self.fb
        if fb is None:
            return
        w = self.img.w * self.scale
        h = self.img.h * self.scale
        self.ox = (fb.width - appbar.SIDEBAR_W) // 2 - w // 2
        self.oy = (fb.height - STATUS_H) // 2 - h // 2
        self._clamp()

    def _zoom_at(self, px, py, factor):
        fb = self.fb
        if fb is None:
            return
        cx = (px - self.ox) / self.scale
        cy = (py - self.oy) / self.scale
        n = max(0.0625, min(16.0, self.scale * factor))
        n = self._snap(n)
        if n == self.scale:
            return
        self.scale = n
        self.ox = px - cx * n
        self.oy = py - cy * n
        self._clamp()
        self.redraw()

    def _zoom(self, dirn):
        """+ / - keyboard zoom: step through the ladder, keep the center."""
        fb = self.fb
        if fb is None:
            return
        if (self.scale <= ZOOM_STEPS[0] + 1e-9 and dirn < 0) \
                or (self.scale >= ZOOM_STEPS[-1] - 1e-9 and dirn > 0):
            return
        i = ZOOM_STEPS.index(self.scale)
        n = ZOOM_STEPS[max(0, min(len(ZOOM_STEPS) - 1, i + dirn))]
        if n == self.scale:
            return
        cx = self.ox + self.img.w * self.scale / 2
        cy = self.oy + self.img.h * self.scale / 2
        self.scale = n
        self.ox = cx - self.img.w * n / 2
        self.oy = cy - self.img.h * n / 2
        self._clamp()
        self.redraw()

    def _pan(self, dx, dy):
        self.ox += dx
        self.oy += dy
        self._clamp()
        self.redraw()

    def _clamp(self):
        fb = self.fb
        if fb is None:
            return
        img = self.img
        w = img.w * self.scale
        h = img.h * self.scale
        x_min = appbar.SIDEBAR_W
        x_max = fb.width - appbar.right_sidebar_w()
        y_min = TITLE_H
        y_max = fb.height - STATUS_H
        if w <= x_max - x_min:
            self.ox = (x_min + x_max) // 2 - w // 2
        else:
            self.ox = min(x_min + MARGIN, max(self.ox, x_max - MARGIN - w))
        if h <= y_max - y_min:
            self.oy = (y_min + y_max) // 2 - h // 2
        else:
            self.oy = min(y_min + MARGIN, max(self.oy, y_max - MARGIN - h))

    def draw_region(self, fb, ox, oy, rw, rh):
        buffered, target = self._begin_shadow(fb, ox, oy, rw, rh)
        if target is None:
            return
        sx, sxr, y = self._draw_app_frame(target, ox, oy, rw, rh)
        self.img.blit(target, self.ox, self.oy, self.scale)
        # status bar: file size, zoom, hints
        sy = fb.height - STATUS_H
        target.fill_rect(sx, sy, fb.width - sx - sxr, STATUS_H, 10, 13, 18)
        info = "%dx%d  %.0f%%  [drag pan / wheel zoom / + - / Home fit / Esc quit]"
        info = info % (self.img.w, self.img.h, self.scale * 100)
        textmod.draw_text(target, sx + 6, sy + 3, info, (150, 160, 175))
        self._end_shadow(fb, target, buffered)

    # ---- mouse (widget-less root) -----------------------------------

    def _on_mouse_any(self, ev):
        x, y, button, pressed, dbl, clicks, wheel = ev
        if wheel:
            self._zoom_at(x, y, 1.25 if wheel > 0 else 0.8)
            return
        if button == 1:
            if pressed:
                self._drag0 = (x, y)
                # clicking fits at scale 1 shows the pixel under a crosshair
            else:
                self._drag0 = None
            return
        if self._drag0 is None:
            return
        dx = x - self._drag0[0]
        dy = y - self._drag0[1]
        if dx or dy:
            self._pan(dx, dy)
            self._drag0 = (x, y)
        if button == 0 and dx == 0 and dy == 0:
            self._drag0 = (x, y)

    def on_key(self, ev):
        if self.destroyed:
            return
        name, ch, pressed = ev
        if not pressed:
            return
        if name == 'esc' or ch in (ord('q'), ord('Q')):
            self._quit()
            return
        if name == 'left':
            self._pan(-24, 0)
            return
        if name == 'right':
            self._pan(24, 0)
            return
        if name == 'up':
            self._pan(0, -24)
            return
        if name == 'down':
            self._pan(0, 24)
            return
        if ch == 43 or ch == 61:  # '+', '='
            self._zoom(1)
            return
        if ch == 45 or ch == 95:  # '-', '_'
            self._zoom(-1)
            return
        if name == 'home':
            self._fit()
            self.redraw()
            return
        tk.Tk.on_key(self, ev)

    def _quit(self):
        if self.destroyed:
            return
        kern.write("imgview: quit\n")
        try:
            self.caller_kb.activate()
        except AttributeError:
            kern.on_key(self.caller_kb._dispatch)
        self.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    """Start the viewer.  kb is the CALLER keyboard (restored on quit);
    returns immediately (the kernel event loop drives the UI)."""
    if path is None:
        img = demo_image()
        label = "(demo)"
    else:
        kern.write("imgview: loading %s\n" % path)
        img = load_image(path)
        label = path
    kern.write("imgview: %dx%d %s\n" % (img.w, img.h, label))
    kb2 = Keyboard()
    root = Viewer(img, label, kb, on_quit)
    # app window system: title-bar '-'/'X' + left sidebar
    root._prev_kb = kb
    root._back = on_quit
    root._close_handler = root._quit
    root.start(fb, kb2, mouse)
    root.mainloop()
    kern.write("imgview: viewer ready (event-driven)\n")