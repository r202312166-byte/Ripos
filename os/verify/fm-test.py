# fm-test.py -- host gate for the M9 file manager (no OS boot).
# Stubs kern + the VFS (a fake tree), runs the real fm.FileManager on a
# fake framebuffer, and asserts Explorer-like behavior: folders first,
# navigation (enter/up/back/forward/home), keyboard + mouse selection,
# and quitting back to the caller.

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
        self._tick = 0

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


kern = _Kern()
sys.modules['kern'] = kern
import os as _real_os
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra").resolve()))
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra/apps").resolve()))

# path -> {name: (isdir, size)}
FAKE = {
    "/": {
        "boot.py": (False, 100),
        "zeta.txt": (False, 5),
        "data.bin": (False, 999),
        "docs.zip": (False, 5000),
        "notes.rtf": (False, 400),
        "Apps": (True, 0),
        "Lib": (True, 0),
        "home": (True, 0),
    },
    "/Apps": {
        "fm.py": (False, 4000),
        "editor.py": (False, 9000),
        "notes.txt": (False, 12),
    },
    "/Lib": {
        "json.py": (False, 3000),
        "Sub": (True, 0),
    },
    "/Lib/Sub": {"deep.txt": (False, 1)},
    "/home": {},
}

MKDIRS = []   # recorded os.mkdir calls (the fake FS stays read-only)

def fake_mkdir(p):
    MKDIRS.append(p)
    return None

_real_os.mkdir = fake_mkdir

def fake_exists(p):
    """Names collide if they exist in the fake tree OR were just made."""
    if p in MKDIRS:
        return True
    head = p.rsplit("/", 1)[0] or "/"
    name = p.rsplit("/", 1)[1]
    d = FAKE.get(head) or FAKE.get("/" + head) or {}
    return name in d

_real_os.path.exists = fake_exists

def _info(p):
    head = p.rsplit("/", 1)[0] or "/"
    name = p.rsplit("/", 1)[1]
    d = FAKE.get(head) or FAKE.get("/" + head) or {}
    return d.get(name)

def fake_listdir(p):
    if p == "/":
        return sorted(FAKE["/"])
    return sorted(FAKE.get(p, {}))

def fake_isdir(p):
    if p == "/":
        return True
    info = _info(p)
    return bool(info and info[0])

def fake_getsize(p):
    info = _info(p)
    return info[1] if info else 0

_real_os.listdir = fake_listdir
_real_os.path.isdir = fake_isdir
_real_os.path.getsize = fake_getsize

import fm
import tkinter as tk
import tk as tkmod
import mouse
from framebuffer import Framebuffer
from keyboard import Keyboard


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)

fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)
root = tk.Tk(title="fm test")
root.start(fb, kb, ms)
quit_called = []
app = fm.FileManager(root, on_quit=lambda: quit_called.append(1))

# root lists folders first, then files, sorted
labels = [app.listbox.items[i][0] for i in range(len(app.listbox.items))]
check(labels == ["Apps/", "home/", "Lib/", "boot.py", "data.bin",
                 "docs.zip", "notes.rtf", "zeta.txt"], labels)
check(app.path == "/", app.path)

# keyboard: the listbox owns the initial focus; down selects, Enter opens
check(root._focused() is app.listbox, root._focused())
kb._dispatch(("down", 0, 1)); kb._dispatch(("down", 0, 0))
check(app.listbox.selected == 0, app.listbox.selected)
kb._dispatch(("down", 0, 1)); kb._dispatch(("down", 0, 0))
check(app.listbox.selected == 1, app.listbox.selected)
kb._dispatch(("down", 0, 1)); kb._dispatch(("down", 0, 0))
check(app.listbox.selected == 2, app.listbox.selected)
kb._dispatch(("enter", 13, 1)); kb._dispatch(("enter", 13, 0))
check(app.path == "/Lib", "enter did not open: %r" % app.path)
labels = [app.listbox.items[i][0] for i in range(len(app.listbox.items))]
check(labels == ["Sub/", "json.py"], labels)

# mouse click selects a row; double-click opens it (row 0 = "Sub/")
row = app.listbox._top + 0
y = app.listbox.y + app.listbox.pady + row * 16
app.listbox.on_click(app.listbox.x + 10, y, 1, 0)
check(app.listbox.selected == 0, app.listbox.selected)
app.listbox.on_click(app.listbox.x + 10, y, 1, 1)   # double click
check(app.path == "/Lib/Sub", "dblclick did not open: %r" % app.path)

# back / forward / up / home
app.go_back()
check(app.path == "/Lib", app.path)
app.go_forward()
check(app.path == "/Lib/Sub", app.path)
app.go_up()
check(app.path == "/Lib", app.path)
app.go_home()
check(app.path == "/", app.path)

# activating a non-editable file just updates the status bar
app.refresh()
labels = [app.listbox.items[i][0] for i in range(len(app.listbox.items))]
idx = labels.index("data.bin")
app.listbox.selected = idx
app.activate(idx, app.listbox.items[idx])
check("data.bin" in app.status.text, app.status.text)
# activating an editable file routes to the editor launch path
idx2 = labels.index("zeta.txt")
app.listbox.selected = idx2
app.activate(idx2, app.listbox.items[idx2])
check(any("opening editor on /zeta.txt" in s for s in kern.log), kern.log)

# .rtf/.docx route to the editor (it is format-aware)
kern.log.clear()
idx3 = labels.index("notes.rtf")
app.listbox.selected = idx3
app.activate(idx3, app.listbox.items[idx3])
check(any("opening editor on /notes.rtf" in s for s in kern.log), kern.log)

# archives route to the archive viewer
kern.log.clear()
idx4 = labels.index("docs.zip")
app.listbox.selected = idx4
app.activate(idx4, app.listbox.items[idx4])
check(any("opening archive viewer on /docs.zip" in s for s in kern.log),
      kern.log)

# New folder works from a read-only dir: it jumps to /home, creates the
# folder there, and navigates so the user sees it
kern.log.clear()
app.go_home()   # back at "/"
app.new_folder()
check(MKDIRS and MKDIRS[-1] == "/home/New Folder", MKDIRS)
check(app.path == "/home", "new folder should navigate to /home: %r"
      % app.path)
# and from a writable dir it stays put
app.new_folder()
check(MKDIRS and MKDIRS[-1] == "/home/New Folder 2", MKDIRS)
check(app.path == "/home", app.path)

# mouse move + click on the toolbar Quit button quits the app
qbtn = app.btn_quit
app.listbox.on_click = lambda *a: None   # avoid stray opens
qbtn.on_click(qbtn.x + 4, qbtn.y + 4, 1, 0)
check(quit_called == [1], quit_called)
check(root.destroyed, "quit did not destroy the root")

# status/path updates are visible in the log markers
check(any("fm: /" in s for s in kern.log), kern.log)

# ---- the full run() path: root.start must happen before building the
# tree, or the lazy _ensure would create a second Keyboard and steal
# kern.on_key from fm's own keyboard (no keys would ever arrive).
quit2 = []
import fm as fmmod
fmmod.run(fb, kb, ms, on_quit=lambda: quit2.append(1))
root2 = tkmod._LAST_ROOT
check(root2 is not None and not root2.destroyed, "fm.run did not create a root")
kb2 = root2.kb
kb2._dispatch(("down", 0, 1)); kb2._dispatch(("down", 0, 0))
check(root2._focused() is not None and root2._focused().selected == 0,
      "fm keyboard dead after run(): %r" % (root2._focused(),))
kb2._dispatch(("esc", 0, 1)); kb2._dispatch(("esc", 0, 0))
check(quit2 == [1], "fm.run quit path broken: %r" % quit2)
check(root2.destroyed, "fm.run quit did not destroy the root")

print("FM-TEST: all assertions passed")
