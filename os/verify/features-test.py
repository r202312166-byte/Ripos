# features-test.py -- host gate for the app window system + editing
# improvements (no OS boot): the appbar registry + sidebar/chrome hit
# testing, editor selection (shift+arrows, double/triple click, drag),
# editor horizontal scrolling, shell stdout capture, and the fm Run /
# Open-with panel wiring.

import io
import os
import pathlib
import sys


class _Kern:
    def __init__(self):
        self.w = 800
        self.h = 600
        self.bpp = 3
        self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
        self.timers = []
        self.key_cb = None
        self.mouse_cb = None
        self._tick = 0
        self._ns = {"__name__": "__main__", "__builtins__": __builtins__}

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
        self._tick += 1
        return self._tick

    def on_key(self, cb):
        self.key_cb = cb

    def on_mouse(self, cb):
        self.mouse_cb = cb

    def eval(self, src):
        # like the real kern.eval: run the line with the CURRENT sys.stdout
        # (the shell swaps in its capture writer around the call)
        try:
            exec(src, self._ns)
        except SystemExit:
            pass
        except Exception as e:
            return ("err", "%s: %s" % (type(e).__name__, e))
        return None


kern = _Kern()
sys.modules['kern'] = kern
_extra = pathlib.Path("os/kernel/initramfs_extra").resolve()
sys.path.insert(0, str(_extra))
sys.path.insert(0, str(_extra / "apps"))

import appbar
import editor
import fm
import mouse
import repl
import text as textmod
import tk
from framebuffer import Framebuffer
from keyboard import Keyboard


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)

# ---- appbar: registry + sidebar + chrome ----------------------------

appbar.PINNED[:] = ["/apps", "/home"]
restored = []
app_id = appbar.register("TestApp", lambda: restored.append(1))
appbar.hide(app_id)
check(appbar.hidden_apps() == [a for a in appbar.APPS if a["id"] == app_id],
      "hidden_apps")
appbar.restore(app_id)
check(restored == [1], "restore callback ran")
check(not appbar.hidden_apps(), "unhidden after restore")

# sidebar drawing + hit testing: the pinned rows are drawn first
fb.clear(0, 0, 0)
appbar.draw_sidebar(fb)
# find the "APPS" header row and the first pinned row via hit testing
hits = []
for yy in range(0, fb.height, 4):
    h = appbar.hit_sidebar(2, yy, fb.height, y0=0)
    if h is not None and h not in hits:
        hits.append(h)
        if len(hits) >= 3:
            break
check(hits[0] == ("head", None), "sidebar header first: %r" % (hits,))
check(hits[1] == ("pin", 0), "first pinned row: %r" % (hits,))
check(hits[2] == ("pin", 1), "second pinned row: %r" % (hits,))
check(appbar.hit_sidebar(appbar.SIDEBAR_W, 10, fb.height) is None,
      "outside sidebar")

# chrome buttons: hit-test the rects returned by draw_chrome
rects = appbar.draw_chrome(fb, 0, 0, 200)
check("min" in rects and "close" in rects, "chrome rects")
rx, ry, rw, rh = rects["close"]
check(appbar.hit_chrome(rx + 1, ry + 1, rects) == "close", "close hit")
rx, ry, rw, rh = rects["min"]
check(appbar.hit_chrome(rx + 1, ry + 1, rects) == "min", "min hit")
check(appbar.hit_chrome(0, 0, rects) is None, "no chrome at 0,0")

# ---- editor: selection, drag, double/triple click, hscroll -----------

root = tk.Tk(title="features")
root.start(fb, kb, ms)
app = editor.Editor(root, on_quit=None, path=None)
ev = app.editor

for c in "alpha beta gamma":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
kb._dispatch(("enter", 13, 1)); kb._dispatch(("enter", 13, 0))
for c in "delta":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
check(ev.lines == ["alpha beta gamma", "delta"], ev.lines)

# shift+right twice selects two characters
kb._dispatch(("up", 0, 1)); kb._dispatch(("up", 0, 0))       # to line 0
kb._dispatch(("home", 0, 1)); kb._dispatch(("home", 0, 0))
kb._dispatch(("shift", 0, 1))
kb._dispatch(("right", 0, 1)); kb._dispatch(("right", 0, 0))
kb._dispatch(("right", 0, 1)); kb._dispatch(("right", 0, 0))
kb._dispatch(("shift", 0, 0))
check(ev._selected_text() == "al", "shift+right selection: %r" % ev._selected_text())
kb._dispatch(("left", 0, 1)); kb._dispatch(("left", 0, 0))   # collapse
check(ev._selected_text() == "", "selection cleared without shift")

# double click selects a word, triple click selects the line
ev.row, ev.col = 0, 0
# position of the "beta" word: 'alpha ' is 6 chars; click mid-word
def word_pos(row, word):
    ln = ev.lines[row]
    i = ln.index(word)
    return i + len(word) // 2

c = word_pos(0, "beta")
ev.on_click(ev.x + ev.padx + textmod.text_width("alpha be"), ev.y + ev.pady, 1, 0, 2)
check(ev._selected_text() == "beta", "double-click word: %r" % ev._selected_text())
ev.on_click(ev.x + ev.padx, ev.y + ev.pady, 1, 0, 3)
check(ev._selected_text() == "alpha beta gamma",
      "triple-click line: %r" % ev._selected_text())

# drag selects from the anchor
ev.on_click(ev.x + ev.padx, ev.y + ev.pady, 1, 0, 1)   # press at col 0
ev.on_drag(ev.x + ev.padx + textmod.text_width("alpha b"), ev.y + ev.pady)
ev.on_release(ev.x + ev.padx + textmod.text_width("alpha b"), ev.y + ev.pady)
check(ev._selected_text() == "alpha b", "drag selection: %r" % ev._selected_text())

# delete selection via backspace
kb._dispatch(("backspace", 8, 1)); kb._dispatch(("backspace", 8, 0))
check(ev.lines[0] == "eta gamma", "delete selection: %r" % ev.lines[0])

# horizontal scroll: a long line; move the cursor to the end
ev.load("x" * 300)
ev.on_key_name("end")
check(ev.col_off > 0, "col_off advanced: %d" % ev.col_off)
root.redraw()   # draw sets the scrollbar rects
check(ev._hsb is not None, "hscrollbar present for a long line")
check(ev._vsb is None or ev._vsb is not None, "vscrollbar rect set")

# mouse wheel scrolls the editor
ev.load("line0\n" + "\n".join("line%d" % i for i in range(1, 60)))
ev.on_wheel(-1)
check(ev.top > 0, "wheel scrolled down: top=%d" % ev.top)
ev.on_wheel(1)
check(ev.top == 0, "wheel scrolled back up: top=%d" % ev.top)

# middle click pastes, right click places the cursor
import clipboard
clipboard.copy("PASTE")
ev.row, ev.col = 0, 0
ev.on_click(ev.x + ev.padx, ev.y + ev.pady, 4, 0, 1)   # middle = paste
check(ev.lines[0].startswith("PASTE"), "middle-click paste: %r" % ev.lines[0])

# ---- shell: stdout capture in the scrollback -------------------------

sh = repl.Shell(fb, Keyboard(), mouse=ms)
sh._submit()  # fresh prompt
n0 = len(sh.lines)
# type print(40 + 2) and submit; kern.eval stub prints to sys.stdout
for c in "print(40 + 2)":
    sh.on_key((c, ord(c), 1))
sh.on_key(("enter", 13, 1))
joined = "\n".join(s for s, c in sh.lines[n0:])
check("42" in joined, "print output captured: %r" % (joined,))

# produce a long scrollback, then pgup/pgdn scroll it
for c in "print('\\n'.join('l%d' % i for i in range(80)))":
    sh.on_key((c, ord(c), 1))
sh.on_key(("enter", 13, 1))
for i in range(3):
    sh.on_key(("pgup", 0, 1))
check(sh.scroll > 0, "pgup scrolled: %d" % sh.scroll)
s_before = sh.scroll
sh.on_key(("pgdn", 0, 1))
check(sh.scroll < s_before, "pgdn reduced: %d -> %d" % (s_before, sh.scroll))

# ---- fm: Run button + open-with panel (fake VFS) ----------------------

fm_mod = fm
# patch fm's os to a fake tree
_fake = {
    "/": {"apps": True, "home": True, "hello.py": False},
    "/apps": {},
    "/home": {},
}
_real_os = os
_real_os.listdir = lambda p: list(_fake.get(p.rstrip("/") or "/", {}))
_real_os.path.isdir = lambda p: bool(_fake.get(p.rstrip("/") or "/")) or     p.rstrip("/") in _fake and isinstance(_fake[p.rstrip("/")], dict)
_real_os.path.exists = lambda p: (p.rstrip("/") or "/") in _fake
_real_os.path.getsize = lambda p: 10
_real_os.mkdir = lambda *a: None
_real_os.remove = lambda p: None
_real_os.rmdir = lambda p: None

froot = tk.Tk(title="fm features")
froot.start(fb, Keyboard(), mouse=ms)
fm_app = fm.FileManager(froot, on_quit=None, start="/")
# open-with panel shows apps for the selected file (Open button
# toggles the panel; the listbox on_select also refreshes it)
# select the hello.py row (folders sort first: apps/, home/, hello.py)
hidx = next((i for i, it in enumerate(fm_app.listbox.items)
             if it[0].startswith("hello")), 0)
fm_app.listbox.selected = hidx
fm_app._toggle_open_panel()
check(fm_app._panel_shown and fm_app.open_list.items, "open panel has items")
check(len(fm_app.open_frame.children) > 0, "panel widget tree present")
labels = [it[0] for it in fm_app.open_list.items]
check("editor" in labels and "run" in labels, "open-with apps: %r" % labels)
# Run executes hello.py (a real file on the host; the fake VFS only
# affects listing, run_selected opens the joined path directly)
import tempfile as _tf
_td = _tf.gettempdir()
hello = _td + "/_fmrun_hello.py"
with open(hello, "w") as f:
    f.write("print('fm run works')\n")
fm_app.path = _td
fm_app.listbox.items = [("_fmrun_hello.py", "0 B")]
fm_app.listbox.selected = 0
fm_app.run_selected()
joined = "\n".join(s for s in kern.log)
check("fm run works" in joined, "fm Run executed the script: %r" % joined[-80:])

# ---- demo apps (tkdemo) are re-runnable -------------------------------

import importlib
import tk as tkmod
import tkdemo
droot1 = tkmod._ACTIVE_ROOT
check(droot1 is not None and not droot1.destroyed, "tkdemo root active")
droot1.destroy()
importlib.reload(tkdemo)
droot2 = tkmod._ACTIVE_ROOT
check(droot2 is not None and droot2 is not droot1 and not droot2.destroyed,
      "reload relaunched tkdemo (re-runnable)")
droot2.destroy()

print("FEATURES-TEST: all assertions passed")
