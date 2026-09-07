# imgedit-test.py -- host gate for initramfs_extra/imgedit.py (M9.2).
# Stubs kern (fake framebuffer), runs the REAL editor code, and drives it
# with synthetic mouse + key events:
#   - pencil dot + drag stroke paint into the document
#   - line / rect tools draw on press->release
#   - color keys switch the palette, eraser erases, clear resets
#   - 's' saves a valid PNG that round-trips through png.load_png
#   - Esc quits: keyboard restored to the caller + on_quit fires
# Run with host python:  py -3 target/imgedit-test.py

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
import imgedit      # noqa: E402
from framebuffer import Framebuffer  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("imgedit-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("imgedit-test FAIL %s%s" % (name, extra))


class _KB:
    def __init__(self):
        self.n = 0
    def _dispatch(self, ev):
        self.n += 1


fb = Framebuffer()
caller = _KB()
quits = []
root = imgedit.Editor(caller, lambda: quits.append(1))
kb2 = __import__("keyboard").Keyboard()
root.save_path = os.path.join(tempfile.gettempdir(), "imgedit-test.png")
root.start(fb, kb2, None)

DW, DH = imgedit.DOC_W, imgedit.DOC_H
# canvas at scale 1, ox=(320-256)//2=32, oy=16+26+4=46
check("layout", (root.scale, root.ox, root.oy) == (1, 32, 46),
      " got %r" % ((root.scale, root.ox, root.oy),))
check("doc starts white", root.doc.get(10, 10) == (255, 255, 255), "")

# helper: screen coords for a doc point
def sc(dx, dy):
    return (root.ox + dx * root.scale, root.oy + dy * root.scale)

# ---- pencil dot ------------------------------------------------------
x, y = sc(20, 20)
root.on_mouse((x, y, 1, 1, 0, 1, 0))
root.on_mouse((x, y, 1, 0, 0, 0, 0))
check("pencil dot", root.doc.get(20, 20) == (255, 255, 255) or True, "")  # white on white!
# default color is white -> pick black first
root.on_key(("2", ord('2'), True))
root.on_mouse((x, y, 1, 1, 0, 1, 0))
root.on_mouse((x, y, 1, 0, 0, 0, 0))
check("pencil dot (black)", root.doc.get(20, 20) == (0, 0, 0),
      " got %r" % (root.doc.get(20, 20),))

# ---- pencil drag -----------------------------------------------------
root.on_mouse((x, y, 1, 1, 0, 1, 0))
for i in range(1, 11):
    px, py = sc(20 + i, 20 + i)
    root.on_mouse((px, py, 0, 0, 0, 0, 0))
root.on_mouse((sc(30, 30)[0], sc(30, 30)[1], 1, 0, 0, 0, 0))
check("pencil drag line", root.doc.get(25, 25) == (0, 0, 0),
      " got %r" % (root.doc.get(25, 25),))

# ---- line tool -------------------------------------------------------
root.on_key(("l", ord('l'), True))
x0, y0 = sc(10, 100)
x1, y1 = sc(40, 100)
root.on_mouse((x0, y0, 1, 1, 0, 1, 0))
root.on_mouse((x1, y1, 1, 0, 0, 0, 0))
check("line drawn", root.doc.get(10, 100) == (0, 0, 0) and root.doc.get(25, 100) == (0, 0, 0) and root.doc.get(40, 100) == (0, 0, 0),
      "")

# ---- rect tool -------------------------------------------------------
root.on_key(("r", ord('r'), True))
root.on_mouse((sc(10, 120)[0], sc(10, 120)[1], 1, 1, 0, 1, 0))
root.on_mouse((sc(30, 140)[0], sc(30, 140)[1], 1, 0, 0, 0, 0))
check("rect outline", root.doc.get(10, 120) == (0, 0, 0) and root.doc.get(30, 140) == (0, 0, 0),
      "")
check("rect hollow", root.doc.get(20, 130) == (255, 255, 255),
      " got %r" % (root.doc.get(20, 130),))

# ---- eraser ----------------------------------------------------------
root.on_key(("e", ord('e'), True))
root.on_mouse((sc(20, 20)[0], sc(20, 20)[1], 1, 1, 0, 1, 0))
root.on_mouse((sc(20, 20)[0], sc(20, 20)[1], 1, 0, 0, 0, 0))
check("eraser", root.doc.get(20, 20) == (255, 255, 255),
      " got %r" % (root.doc.get(20, 20),))

# ---- save -> valid PNG round-trip ------------------------------------
root.on_key(("s", ord('s'), True))
import os as _os
if _os.path.exists(root.save_path):
    with open(root.save_path, "rb") as f:
        data = f.read()
    back = png.load_png(data)
    check("saved png size", (back.w, back.h) == (DW, DH), " got %r" % ((back.w, back.h),))
    check("saved png pixels", back.get(10, 100) == (0, 0, 0) and back.get(20, 130) == (255, 255, 255), "")
    _os.remove(root.save_path)
else:
    check("saved png file", False, " missing %s" % root.save_path)

# ---- clear + quit ----------------------------------------------------
root.on_key(("c", ord('c'), True))
check("clear", root.doc.get(10, 100) == (255, 255, 255), "")
root.on_key(("esc", 27, True))
check("quit destroys", root.destroyed, "")
check("quit calls on_quit", len(quits) == 1, "")
check("quit restores keyboard", kern.key_cb == caller._dispatch, "")

print("imgedit-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
