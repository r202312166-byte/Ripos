# imgview-test.py -- host gate for initramfs_extra/imgview.py (M9.2).
# Stubs kern (fake framebuffer), runs the REAL viewer code, and asserts:
#   - load_image roundtrips PNG (png.py) and decodes JPEG (jpeg.py)
#   - the image is blitted at the fitted/centered offset, pixel-exact
#   - background pixels outside the image are the viewer BG
#   - '+'/'-' zoom, arrows pan, Home refits
#   - Esc quits: kernel keyboard restored to the caller + on_quit fires
#   - run() with no path boots the built-in demo image
# Run with host python:  py -3 target/imgview-test.py

import io
import os
import pathlib
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra", "apps"))
class _Kern:
    def __init__(self):
        self.w = 320
        self.h = 240
        self.bpp = 3
        self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
        self.timers = []
        self.key_cb = None
        self.mouse_cb = None

    def fb_info(self):
        return (self.w, self.h, self.stride, self.bpp, 0)

    def fb_mem(self):
        return self.mem

    def fb_set_mode(self, w, h):
        return None

    def write(self, s):
        self.log.append(s)

    def after(self, ms, cb):
        self.timers.append((ms, cb))

    def tick(self):
        return 0

    def on_key(self, cb):
        self.key_cb = cb

    def on_mouse(self, cb):
        self.mouse_cb = cb


kern = _Kern()
sys.modules['kern'] = kern

import png          # noqa: E402
import imgview      # noqa: E402
from framebuffer import Framebuffer  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("imgview-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("imgview-test FAIL %s%s" % (name, extra))


class _KB:
    def __init__(self):
        self.dispatches = 0
    def _dispatch(self, ev):
        self.dispatches += 1


# ---- 1. PNG roundtrip via load_image ---------------------------------
img = png.Image(40, 30, "rgb")
for y in range(30):
    for x in range(40):
        img.set(x, y, ((x * 5) % 256, (y * 7) % 256, (x + y) % 256))
img.set(0, 0, (200, 30, 40))
img.set(39, 29, (10, 250, 5))
tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
tmp.write(png.save_png(img))
tmp.close()
got = imgview.load_image(tmp.name)
ok = (got.w, got.h) == (40, 30) and got.get(0, 0) == (200, 30, 40) and got.get(39, 29) == (10, 250, 5)
check("load_image PNG roundtrip", ok, " got %r %r" % (got.get(0, 0), got.get(39, 29)))

# ---- 2. JPEG decode via load_image -----------------------------------
from PIL import Image as PILImage  # noqa: E402
pj = PILImage.new("RGB", (24, 16), (120, 60, 200))
pjb = io.BytesIO()
pj.save(pjb, "JPEG", quality=90)
tmpj = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
tmpj.write(pjb.getvalue())
tmpj.close()
jimg = imgview.load_image(tmpj.name)
check("load_image JPEG", (jimg.w, jimg.h) == (24, 16), " got %r" % ((jimg.w, jimg.h),))
# solid color decodes close to the source
r, g, b = jimg.get(5, 5)
check("load_image JPEG color", max(abs(r - 120), abs(g - 60), abs(b - 200)) <= 3,
      " got %r" % ((r, g, b),))

# ---- 3. viewer blit placement ----------------------------------------
fb = Framebuffer()
caller = _KB()
quits = []


def on_quit():
    quits.append(1)


root = imgview.Viewer(img, "test.png", caller, on_quit)
kb2 = __import__("keyboard").Keyboard()  # steals kern.on_key
appbar = __import__("appbar")
root.start(fb, kb2, None)
# fit: 40x30 into the cavity right of the 92px sidebar -> scale 5, centered
# ox = (92+320)//2 - 40*5//2 = 106, oy = (16+225)//2 - 30*5//2 = 45
check("fit scale/offset", (root.scale, root.ox, root.oy) == (5.0, 106.0, 45.0),
      " got %r" % ((root.scale, root.ox, root.oy),))
# image pixel (0,0)=(200,30,40) must be at fb(106,45)
check("blit pixel (0,0)", fb.pixel(106, 45) == (200, 30, 40), " got %r" % (fb.pixel(106, 45),))
check("blit pixel (39,29)", fb.pixel(106 + 39 * 5, 45 + 29 * 5) == (10, 250, 5),
      " got %r" % (fb.pixel(106 + 39 * 5, 45 + 29 * 5),))
# background outside the image (but outside the sidebar strip)
check("bg pixel", fb.pixel(315, 200) == __import__("settings").theme()['bg'],
      " got %r" % (fb.pixel(315, 200),))
# title bar present
check("title bar", fb.pixel(160, 4) == imgview.TITLE_BG, " got %r" % (fb.pixel(160, 4),))
# sidebar strip drawn (app window system)
check("sidebar drawn", fb.pixel(4, 100) == appbar.SIDEBAR_BG, " got %r" % (fb.pixel(4, 100),))
# status bar drawn at the bottom
check("status bar", fb.pixel(315, 235) == (10, 13, 18), " got %r" % (fb.pixel(315, 235),))

# ---- 4. zoom / pan / fit ---------------------------------------------
root.on_key(("equal", 43, True))   # '+'
check("zoom in", root.scale == 6, " got %s" % root.scale)
root.on_key(("equal", 43, True))
check("zoom in 2", root.scale == 8, " got %s" % root.scale)
root.on_key(("minus", 45, True))   # '-'
check("zoom out", root.scale == 6, " got %s" % root.scale)
# zoom up so the image is LARGER than the screen, then pan must move it
while root.scale < 9:
    root.on_key(("equal", 43, True))
check("zoom big", root.scale == 12, " got %s" % root.scale)
ox0, oy0 = root.ox, root.oy
root.on_key(("right", 0, True))
check("pan moves", root.ox > ox0, " got %d->%d" % (ox0, root.ox))
root.on_key(("left", 0, True))
root.on_key(("up", 0, True))
check("pan up moves", root.oy < oy0, " got %d->%d" % (oy0, root.oy))
root.on_key(("home", 0, True))
check("home refits", root.scale == 5.0 and root.ox == 106 and root.oy == 45,
      " got %r" % ((root.scale, root.ox, root.oy),))

# ---- 4b. mouse wheel zoom + drag pan (widget-less _mouse_any hooks) ---
root.on_mouse((160, 120, 0, 0, 0, 0, 120))    # wheel up
check("wheel zoom in", root.scale == 6, " got %s" % root.scale)
root.on_mouse((160, 120, 0, 0, 0, 0, 120))
root.on_mouse((160, 120, 0, 0, 0, 0, -120))   # wheel down
check("wheel zoom out", root.scale == 6, " got %s" % root.scale)
# drag to pan: press at (160,120), move to (200,160), release (pan right
# and down; the vertical axis re-centers at scale 6 because the 180px-tall
# image fits the cavity, the horizontal pans because 240px is wider)
sx, so = root.ox, root.oy
root.on_mouse((160, 120, 1, 1, 0, 1, 0))
root.on_mouse((200, 160, 0, 0, 0, 0, 0))
check("drag pans right", root.ox > sx, " got %d->%d" % (sx, root.ox))
check("drag clamps right", root.ox <= 110, " got %d" % (root.ox,))
root.on_mouse((200, 160, 1, 0, 0, 0, 0))
root.on_key(("home", 0, True))

# ---- 5. quit restores the caller keyboard ----------------------------
root.on_key(("esc", 27, True))
check("quit destroys", root.destroyed, "")
check("quit calls on_quit", len(quits) == 1, "")
check("quit restores keyboard", kern.key_cb == caller._dispatch, "")

# ---- 5b. RGBA load flattening (transparency over checkerboard) --------
rgba = png.Image(4, 2, "rgba")
rgba.pixels[:] = bytes((255, 0, 0, 255,  0, 0, 255, 128,
                        0, 255, 0, 0,   10, 20, 30, 64,
                        255, 255, 0, 128, 1, 2, 3, 4,
                        5, 6, 7, 8,   9, 10, 11, 12))
tmp_rgba = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
tmp_rgba.write(png.save_png(rgba))
tmp_rgba.close()
flat = imgview.load_image(tmp_rgba.name)
check("load_image flattens rgba->rgb", flat.mode == "rgb", " got %s" % flat.mode)
check("flatten opaque keeps color", flat.get(0, 0) == (255, 0, 0),
      " got %r" % (flat.get(0, 0),))
check("flatten transparent shows checker", flat.get(2, 0) != (0, 255, 0),
      " got %r" % (flat.get(2, 0),))

# ---- 6. run() with no path = demo image -------------------------------
kern.mem = memoryview(bytearray(kern.w * kern.h * kern.bpp))
kern.key_cb = None
imgview.run(fb, caller, None, on_quit=on_quit, path=None)
check("run demo draws", len(kern.log) > 0 and "imgview:" in kern.log[-1], "")
# demo image is 192x108 -> fit scale lands on a ladder step; blit present
check("demo present on screen", any(fb.pixel(x, 200) != imgview.BG for x in range(92, 320)),
      "")

print("imgview-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
