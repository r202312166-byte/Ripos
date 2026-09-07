# boot-smoke-test.py -- host gate: the WHOLE boot.py runs without error
# (registers the codecs, creates the framebuffer/keyboard/mouse and the
# shell, and wires the appbar fallback + base keyboard).  Catches e.g. a
# missing import in boot.py that only shows up at OS boot.

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

# run the real boot.py end to end (it imports the real drivers + shell)
src = open(_extra / "boot.py").read()
ns = {"__name__": "boot"}
exec(compile(src, "boot.py", "exec"), ns)

import keyboard as kbd
import appbar

assert kbd._BASE is not None, "base keyboard not registered"
assert appbar._fallback_redraw is not None, "fallback redraw not registered"
print("BOOT-SMOKE-TEST: boot.py ran clean, shell + fallback wired")
