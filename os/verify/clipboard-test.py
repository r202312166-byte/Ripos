# clipboard-test.py -- M9.6 gate: clipboard, Ctrl hotkeys, Ctrl+Z undo.
# Stubs kern, runs the REAL editor + tk hotkey pre-pass, and drives them
# with synthetic keyboard events.
import os
import pathlib
import sys


class _Kern:
    def __init__(self):
        self.w = 640
        self.h = 480
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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra", "apps"))

import editor  # noqa: E402
import tkinter as tk  # noqa: E402
import clipboard  # noqa: E402
from framebuffer import Framebuffer  # noqa: E402
from keyboard import Keyboard  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("clipboard-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("clipboard-test FAIL %s%s" % (name, extra))


fb = Framebuffer()
kb = Keyboard()
root = tk.Tk(title="clipboard test")
root.start(fb, kb, None)
app = editor.Editor(root, on_quit=None, path=None)


def type_keys(s):
    for c in s:
        kb._dispatch((c, ord(c), 1))
        kb._dispatch((c, ord(c), 0))


def ctrl_key(c):
    kb.pressed["ctrl"] = True
    kb._dispatch((c.lower(), ord(c.lower()), 1))
    kb._dispatch((c.lower(), ord(c.lower()), 0))
    kb.pressed["ctrl"] = False


# typing + undo
type_keys("abc")
check("typing", app.editor.get_text() == "abc", " got %r" % app.editor.get_text())
ctrl_key("z")
check("undo 1", app.editor.get_text() == "ab", " got %r" % app.editor.get_text())
ctrl_key("z")
ctrl_key("z")
check("undo all", app.editor.get_text() == "", " got %r" % app.editor.get_text())

# copy all + paste
type_keys("hello")
ctrl_key("c")
check("copy all", clipboard.get() == "hello", " got %r" % clipboard.get())
ctrl_key("v")
check("paste", app.editor.get_text() == "hellohello", " got %r" % app.editor.get_text())

# cut
ctrl_key("x")
check("cut", app.editor.get_text() == "" and clipboard.get() == "hellohello",
      " text=%r clip=%r" % (app.editor.get_text(), clipboard.get()))

# paste in the middle
type_keys("Ripos ")
ctrl_key("v")
check("paste again", app.editor.get_text() == "Ripos hellohello",
      " got %r" % app.editor.get_text())

# tk hotkey pre-pass: unbound control+letter must NOT type, bound must fire
fired = []
root.bind("<Control-a>", lambda e: fired.append(1))
kb.pressed["ctrl"] = True
kb._dispatch(("a", 97, 1))
kb.pressed["ctrl"] = False
check("ctrl+a hotkey fired", len(fired) == 1, "")
check("ctrl+a did not type", app.editor.get_text() == "Ripos hellohello",
      " got %r" % app.editor.get_text())

print("clipboard-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)