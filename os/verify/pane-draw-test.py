# pane-draw-test.py -- exercise the REAL draw path of the editor console pane.
import pathlib, sys
class _Kern:
    def __init__(self):
        self.w = 1280; self.h = 1024; self.bpp = 3; self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []; self.timers = []; self.key_cb = None; self.mouse_cb = None; self._tick = 0
    def fb_info(self): return (self.w, self.h, self.stride, self.bpp, 0)
    def fb_mem(self): return self.mem
    def fb_set_mode(self, w, h): return None
    def write(self, s): self.log.append(s)
    def after(self, ms, cb): self.timers.append((ms, cb))
    def tick(self): self._tick += 1; return self._tick
    def on_key(self, cb): self.key_cb = cb
    def on_mouse(self, cb): self.mouse_cb = cb
kern = _Kern()
sys.modules["kern"] = kern
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra").resolve()))
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra/apps").resolve()))
import tk, editor
from framebuffer import Framebuffer
from keyboard import Keyboard
import mouse
fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)
root = tk.Tk(title="pane test")
root.start(fb, kb, ms)
app = editor.Editor(root, on_quit=None, path=None)

# focus the pane, type print(1+1), Enter
fs = root._focusables()
root._focus_idx = fs.index(app.pane)
for c in "print(1+1)":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
kb._dispatch(("enter", 13, 1)); kb._dispatch(("enter", 13, 0))

# now the pane lines
print("PANELINES:", app.pane.lines)

# draw the real tree into the framebuffer
root.redraw()

# find pixels in the pane region that are bright (fg ~ (225,230,238))
def near(c, t, tol=30): return all(abs(a-b) <= tol for a, b in zip(c, t))
found_fg = []
for y in range(0, 1024):
    for x in range(0, 1280, 2):
        o = (y * 1280 + x) * 3
        c = (kern.mem[o+2], kern.mem[o+1], kern.mem[o])  # bgr->rgb
        if near(c, (225, 230, 238), 60) or near(c, (120, 200, 120), 60):
            found_fg.append((x, y, c))
print("bright pixels:", len(found_fg))
# bucket by y to find text rows in the pane (y 847..997)
rows = {}
for x, y, c in found_fg:
    rows.setdefault(y // 16, []).append((x, c))
for k in sorted(rows):
    xs = [p[0] for p in rows[k]]
    print("row-band %d (y %d..%d): %d px, x %d..%d" % (k, k*16, k*16+15, len(rows[k]), min(xs), max(xs)))