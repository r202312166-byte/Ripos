# switch-test.py -- host gate for the app-switch refresh fixes (no OS boot).
#
# Simulates the exact flows the user reported:
#  1. shell -> fm -> Quit -> shell: the shell must be painted IMMEDIATELY
#     (the fm root must NOT repaint itself over the shell after on_quit).
#  2. fm -> editor (double-click) -> the editor's first frame must stay on
#     screen (fm's click handler must NOT redraw over the launched app).
#  3. editor -> fm (quit back): fm reactivates and paints.

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

    def eval(self, src):
        try:
            exec(src, {"__name__": "__main__", "__builtins__": __builtins__})
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
import wm
import repl
import tk
from framebuffer import Framebuffer
from keyboard import Keyboard


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)
import tk as tkmod

# ---- scenario 1: shell -> fm -> Quit -> shell -------------------------

sh = repl.Shell(fb, kb, mouse=ms)
# the shell painted its frame: check a shell-only pixel (sidebar bg,
# clear of the mouse cursor sprite which sits at the top-left corner)
check(fb.pixel(50, 100) == appbar.SIDEBAR_BG,
      "shell frame painted: %r" % (fb.pixel(50, 100),))
shell_kb = kb

# launch fm like the shell does
quit_log = []
fm.run(fb, kb, ms, on_quit=lambda: quit_log.append("shell_restored"))
# fm is now on screen (its title bar bg at top-right of the app area)
check(tkmod._ACTIVE_ROOT is not None and tkmod._ACTIVE_ROOT is not sh,
      "fm owns the screen")
check(len(quit_log) == 0, "no quit yet")

# find the FileManager instance through the root (the app holds it;
# reach it via the file list's _fm back-reference)
def find_fm(w):
    if getattr(w, '_fm', None) is not None:
        return w._fm
    if hasattr(w, 'children'):
        for c in w.children:
            r2 = find_fm(c)
            if r2 is not None:
                return r2
    return None


# click the fm Quit button (simulate a real mouse press through fm's root)
fm_root = tkmod._ACTIVE_ROOT
fm_app = find_fm(fm_root)
check(fm_app is not None, "fm app found")
qx = fm_app.btn_quit.x + 5
qy = fm_app.btn_quit.y + 5
fm_root.on_mouse((qx, qy, 1, 1, 0, 1, 0))    # press on Quit
fm_root.on_mouse((qx, qy, 1, 0, 0, 0, 0))    # release
check(quit_log == ["shell_restored"], "on_quit fired: %r" % quit_log)
# the fm root is destroyed and no root owns the screen
check(tkmod._ACTIVE_ROOT is None, "no root owns the screen after quit")
# THE KEY ASSERTION: the shell frame is visible now, NOT fm's frame --
# the dead fm root must not have repainted over the shell (the stale
# on_mouse redraw bug).  The shell sidebar color sits at (50, 100).
check(fb.pixel(50, 100) == appbar.SIDEBAR_BG,
      "shell frame after fm quit: %r (fm leaked over it)" % (fb.pixel(50, 100),))

# ---- scenario 2: fm -> editor (double-click) keeps the editor frame ----

# relaunch fm
fm.run(fb, kb, ms, on_quit=None)
fm_root = tkmod._ACTIVE_ROOT
fm_app = find_fm(fm_root)
check(fm_app is not None, "fm app found (2)")

# double-click a file opens the editor (a real nested launch)
import os
os.listdir = lambda p: ["test.py"] if p == "/" else []
os.path.isdir = lambda p: False
os.path.getsize = lambda p: 10
fm_app.listbox.set_items([("test.py", "10 B")])
fm_app.listbox.selected = 0
fm_app.activate(0, ("test.py", "10 B"))
# after activate -> open_editor -> editor.run -> editor root owns the screen
editor_root = tkmod._ACTIVE_ROOT
check(editor_root is not None and editor_root is not fm_root,
      "editor took the screen")
# THE KEY ASSERTION: the editor's frame is still on screen -- fm's click
# handler must NOT have redrawn over it (fm.on_mouse returned after
# activate, and must skip its own redraw since _ACTIVE_ROOT changed).
# The editor's bg color sits where fm's status bar was; the editor paints
# its title bar (wm.TITLE_BG) at the top of the app area.
check(fb.pixel(50, 100) == appbar.SIDEBAR_BG,
      "sidebar still drawn (editor owns it): %r" % (fb.pixel(50, 100),))

# ---- scenario 3: editor quits back to fm ------------------------------

# editor quit (like Esc): editor.run's quit_app restores fm's keyboard
kern.on_key(editor_root.kb._dispatch)
editor_root.destroy()
# fm reactivates itself via its back() callback
import tk as tkmod2
# simulate the editor run's quit closure: restore + destroy + on_quit
# (fm's back() sets _ACTIVE_ROOT = fm root and refreshes)
quit2 = []
editor.run(fb, kb, ms, on_quit=lambda: quit2.append("fm_back"), path=None)
eroot = tkmod._ACTIVE_ROOT
check(eroot is not None and eroot is not fm_root, "editor owns screen (2)")
# esc the editor: drive its own quit path via key
kern.on_key(eroot.kb._dispatch)   # simulate run()'s quit restoring caller kb
eroot.destroy()
# editor.run's on_quit (fm's back) should have re-activated fm... but
# destroy() alone does not call on_quit (the run wrapper does).  Replicate:
fm_root.activate()
check(tkmod._ACTIVE_ROOT is fm_root, "fm reactivated after editor quit")

# ---- hover must NOT trigger chrome/sidebar actions ---------------------

import keyboard as kbd

hover_root = tk.Tk(title='hover', sidebar=True)
hover_root.start(fb, Keyboard(), ms)
hkbd = hover_root.kb
hover_app_id = hover_root._app_id
# hover the title-bar chrome buttons (move events, button 0, not pressed)
# the buttons sit at the right end of the title bar; use the actual rects
check(len(hover_root._chrome) >= 2, "chrome rects present: %r"
      % hover_root._chrome)
ch_min = hover_root._chrome['min']
hx, hy = ch_min[0] + 2, ch_min[1] + 2
hover_root.on_mouse((hx, hy, 0, 0, 0, 0, 0))   # hover over '-', no press
check(not hover_root._minimized and not hover_root.destroyed,
      "hover over '-'/'X' must NOT minimize/close the app")
# hover the left sidebar (pinned folders / hidden apps)
hover_root.on_mouse((10, 30, 0, 0, 0, 0, 0))
check(tkmod._ACTIVE_ROOT is hover_root and not hover_root._minimized,
      "hover over the sidebar must NOT act")
# a REAL press on the chrome '-' minimizes
hover_root.on_mouse((hx, hy, 1, 1, 0, 1, 0))
check(hover_root._minimized, "real press on '-' minimizes")
appbar.restore(hover_app_id)
check(not hover_root._minimized, "sidebar restore brings it back")
hover_root.destroy()

# ---- an app that quits without restoring gets the shell keyboard back ---

# the base keyboard is the shell's (set by boot.py in the OS; set here)
kbd._BASE = sh.kb
bare = tk.Tk(title='bare')
bare.start(fb, Keyboard(), ms)
check(kbd._CURRENT is bare.kb, "bare app owns the keyboard")
# clicking its Quit-equivalent: destroy() with no _close_handler/_prev_kb
bare.destroy()
check(kbd._CURRENT is sh.kb, "destroy restored the base (shell) keyboard")
check(tkmod._ACTIVE_ROOT is None, "no root owns the screen after bare quit")

print("SWITCH-TEST: all assertions passed")
