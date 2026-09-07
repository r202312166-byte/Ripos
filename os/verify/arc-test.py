# arc-test.py -- host gate for the M9.3 archive viewer (no OS boot).
# Stubs kern, writes a real zip and a real tar to a temp dir, then drives
# arc.Archive (list + read) and the ArcViewer UI (double-click extracts to
# the fake /home VFS).
import io
import pathlib
import sys
import tempfile
import os as _real_os


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
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra").resolve()))
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra/apps").resolve()))
import tk
import tk as tkmod
import arc
import mouse
from framebuffer import Framebuffer
from keyboard import Keyboard


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---- a real zip and a real tar on disk --------------------------------
tmp = tempfile.mkdtemp(prefix="arc-test-")
zip_path = tmp + "/demo.zip"
tar_path = tmp + "/demo.tar"

import zipfile
with zipfile.ZipFile(zip_path, "w") as z:
    z.writestr("hello.txt", b"hello zip")
    z.writestr("sub/data.bin", bytes(range(64)))

import tarfile
with tarfile.open(tar_path, "w") as t:
    ti = tarfile.TarInfo("greet.txt")
    ti.size = 8
    t.addfile(ti, io.BytesIO(b"hi there"))
    ti2 = tarfile.TarInfo("sub/")
    ti2.type = tarfile.DIRTYPE
    t.addfile(ti2)

# ---- fake /home VFS: intercept the builtin open for writes -----------
import builtins
_real_open = builtins.open
WRITES = []


def fake_mkdir(p):
    return None


_real_os.mkdir = fake_mkdir
_real_os.makedirs = lambda p, exist_ok=False: None


def fake_open(p, mode="r", *a, **kw):
    if "w" in mode or "a" in mode or "x" in mode:
        buf = io.BytesIO()
        WRITES.append((p, buf))
        return buf
    return _real_open(p, mode, *a, **kw)


builtins.open = fake_open

# ---- Archive: list + read --------------------------------------------
a = arc.Archive(zip_path)
names = [n for n, s, k in a.names]
check("hello.txt" in names and "sub/data.bin" in names, names)
check(a.read("hello.txt") == b"hello zip", a.read("hello.txt"))
check(a.kind == "zip", a.kind)
a.close()

b = arc.Archive(tar_path)
check(b.kind == "tar", b.kind)
names = [n for n, s, k in b.names]
check("greet.txt" in names, names)
check(b.read("greet.txt") == b"hi there", b.read("greet.txt"))
b.close()

# ---- the viewer UI: builds, lists entries, extract works -------------
fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)
root = tk.Tk(title="arc test")
root.start(fb, kb, ms)
quit_called = []
view = arc.ArcViewer(root, on_quit=lambda: quit_called.append(1),
                     path=zip_path)
labels = [view.listbox.items[i][0] for i in range(len(view.listbox.items))]
check("hello.txt" in labels, labels)
check(view.path_label.text.startswith(zip_path), view.path_label.text)

# select hello.txt and extract it -> the fake /home records the write
idx = labels.index("hello.txt")
view.listbox.selected = idx
view.extract_selected()
check(any("/home" in p for p, b in WRITES), WRITES)

view.quit_app()
check(root.destroyed, "quit did not destroy the root")
check(quit_called == [1], quit_called)

print("ARC-TEST: all assertions passed")
