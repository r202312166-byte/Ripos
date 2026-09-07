# settings-test.py -- host gate for the config system (no OS boot):
# themes (dark/light), language, sidebar sections, right sidebar, the
# config app's actions (theme switch, pinned add/remove).

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
_extra = pathlib.Path("os/kernel/initramfs_extra").resolve()
sys.path.insert(0, str(_extra))
sys.path.insert(0, str(_extra / "apps"))


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


import settings
import appbar
import tk
from framebuffer import Framebuffer
from keyboard import Keyboard

# ---- defaults ----------------------------------------------------------

check(settings.get("theme") == "dark", "default theme dark")
check(settings.theme()["bg"] == (22, 26, 34), "dark bg")
check(settings.c("bg") == (22, 26, 34), "c() dark bg")

# ---- theme switch -------------------------------------------------------

settings.set_theme("light")
check(settings.get("theme") == "light", "theme set to light")
t = settings.theme()
check(t["bg"] == (255, 255, 255), "light bg is white")
check(t["fg"] == (0, 0, 0), "light fg is black")
check(t["title_bg"] == (255, 240, 170), "light title is light yellow")
check(settings.c("panel") == (255, 248, 210), "light panel is light yellow")
# apply_theme re-colored the running modules
import repl
import editor
import wm

check(repl.BG == (255, 255, 255), "repl recolored: %r" % (repl.BG,))
check(editor.BG == (255, 255, 255), "editor recolored")
check(wm.BG == (255, 255, 255), "wm recolored")
check(appbar.SIDEBAR_BG == (255, 246, 200), "appbar recolored")

# a freshly built tk widget tree uses the light palette
fb = Framebuffer()
root = tk.Tk(title="t")
root.start(fb, Keyboard())
lab = tk.Label(root, text="hi")
check(lab.bg == (255, 248, 210), "light label bg: %r" % (lab.bg,))
btn = tk.Button(root, text="b")
check(btn.bg == (255, 240, 170), "light button bg: %r" % (btn.bg,))
check(btn.focus == (200, 150, 20), "light focus ring: %r" % (btn.focus,))
root.destroy()

# back to dark
settings.set_theme("dark")
check(settings.theme()["fg"] == (225, 230, 238), "dark fg restored")
check(repl.BG == (22, 26, 34), "repl dark again: %r" % (repl.BG,))

# ---- sidebar sections + right sidebar -----------------------------------

settings.set("sidebar_left", ["pinned"])          # drop the apps section
settings.set("sidebar_right", "apps")             # hidden apps on the right
check("apps" not in appbar._left_sections(), "left sections configurable")
check(appbar.right_sidebar_w() == appbar.SIDEBAR_R_W, "right sidebar on")
appbar.draw_sidebar_right(fb)
check(fb.pixel(fb.width - 2, 100) == appbar.SIDEBAR_BG, "right sidebar drawn")
settings.set("sidebar_right", "none")
check(appbar.right_sidebar_w() == 0, "right sidebar off")

# ---- config app ---------------------------------------------------------

import config

settings.set("theme", "dark")
settings.set("pinned", ["/apps", "/home", "/"])
croot = tk.Tk(title=config.tr("title"), sidebar=True)
croot.start(fb, Keyboard())
capp = config.ConfigApp(croot, on_quit=None)
check(len(capp.pinned_list.items) >= 3, "pinned list populated")
# add a pinned folder
capp.pinned_entry.set("/media")
capp._add_pinned()
check("/media" in appbar.PINNED, "pinned add: %r" % appbar.PINNED)
# remove it
capp.pinned_list.set_items([(p, "") for p in appbar.PINNED])
capp.pinned_list.selected = appbar.PINNED.index("/media")
capp._remove_pinned()
check("/media" not in appbar.PINNED, "pinned remove")
# theme toggle through the app
capp._set_theme("light")
check(settings.get("theme") == "light", "config app theme switch")
capp._set_lang("zh")
check(settings.get("language") == "zh", "language set to zh")
check(config.tr("theme") == "主题", "zh label: %r" % config.tr("theme"))
capp._set_lang("en")
capp.quit_app()

print("SETTINGS-TEST: all assertions passed")
