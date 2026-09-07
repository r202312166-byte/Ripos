# tk-test.py -- host-side logic test for the tk widget toolkit (no OS boot).
#
# Stubs the kern module, imports the initramfs_extra modules directly on the
# host, drives key events through keyboard.Keyboard, and asserts widget
# behavior: layout, focus cycling, button activation, canvas drawing,
# destroy stopping the redraw timer.

import sys
import pathlib


class _Kern:
    def __init__(self):
        self.w = 320
        self.h = 240
        self.bpp = 3
        # stride is in PIXELS like the real bootloader framebuffer; the
        # driver must compute byte offsets as (y * stride + x) * bpp
        self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
        self.timers = []
        self.key_cb = None

    def fb_info(self):
        return (self.w, self.h, self.stride, self.bpp, 0)

    def fb_mem(self):
        return self.mem

    def write(self, s):
        self.log.append(s)

    def after(self, ms, cb):
        self.timers.append((ms, cb))

    def tick(self):
        return 0

    def on_key(self, cb):
        self.key_cb = cb


kern = _Kern()
sys.modules['kern'] = kern
# resolve the initramfs source tree relative to this script (os/target/..)
_extra = pathlib.Path(__file__).resolve().parents[1] / 'kernel' / 'initramfs_extra'
sys.path.insert(0, str(_extra))

import tk  # noqa: E402
from framebuffer import Framebuffer  # noqa: E402
from keyboard import Keyboard  # noqa: E402

fb = Framebuffer()
kb = Keyboard()
root = tk.Tk(title='tk test')
root.start(fb, kb)

calls = []
status = tk.Label(root, text='hello widgets', fg=(120, 200, 120))
status.pack()
btn = tk.Button(root, text='Go', command=lambda: calls.append('go'))
btn.pack()
cv = tk.Canvas(root, width=120, height=60)
cv.create_line(0, 0, 100, 50, color=(255, 0, 0))
cv.create_rect(10, 10, 30, 20, color=(40, 60, 90))
cv.create_text(60, 30, 'hi', color=(0, 0, 255))
cv.create_pixel(5, 5, color=(255, 255, 0))
cv.pack()
placed = tk.Label(root, text='placed')
placed.place(10, 100)

root.mainloop()

log = kern.log
assert any('tk: mainloop started' in s for s in log), log

# chrome: title bar at the top of the screen
assert fb.pixel(2, 2) == (70, 95, 140), fb.pixel(2, 2)

# canvas primitives (relative to the packed canvas position; the sample
# points avoid the 2x canvas text and the place()d label below)
assert fb.pixel(cv.x + 48, cv.y + 24) == (255, 0, 0), fb.pixel(cv.x + 48, cv.y + 24)      # line
assert fb.pixel(cv.x + 15, cv.y + 15) == (40, 60, 90), fb.pixel(cv.x + 15, cv.y + 15)     # fill rect
assert fb.pixel(cv.x + 5, cv.y + 5) == (255, 255, 0), fb.pixel(cv.x + 5, cv.y + 5)        # pixel
assert fb.pixel(cv.x + 60, cv.y + 10) == (30, 36, 48), fb.pixel(cv.x + 60, cv.y + 10)     # canvas bg

# place() pins absolute geometry: the label draws at (10, 100), overlapping
# the bottom-left of the canvas (drawn later, so it wins there)
assert (placed.x, placed.y) == (10, 100), (placed.x, placed.y)
assert fb.pixel(placed.x + 2, placed.y + 2) == (40, 46, 58), fb.pixel(placed.x + 2, placed.y + 2)

# initial focus is the first focusable widget (btn), drawn with the
# focus ring color
assert root._focused() is btn
assert btn.focused is True
assert fb.pixel(btn.x + 2, btn.y + 2) == (210, 130, 40), fb.pixel(btn.x + 2, btn.y + 2)

# Enter presses the focused button -> command fires, serial marker logged
kb._dispatch(('enter', 13, 1))
kb._dispatch(('enter', 13, 0))
assert calls == ['go'], calls
assert any('tk: button "Go" clicked' in s for s in log), log
assert btn.focused is True
assert fb.pixel(btn.x + 2, btn.y + 2) == (210, 130, 40), fb.pixel(btn.x + 2, btn.y + 2)

# Tab cycles focus (still the only focusable), Enter fires again
kb._dispatch(('tab', 0, 1))
kb._dispatch(('tab', 0, 0))
kb._dispatch(('enter', 13, 1))
kb._dispatch(('enter', 13, 0))
assert calls == ['go', 'go'], calls

# labels update live
status.set_text('new text')
assert status.text == 'new text'

# destroy stops the periodic redraw timer
root.destroy()
assert any('tk: destroyed' in s for s in log), log
n0 = len(kern.timers)
root._tick()
assert len(kern.timers) == n0, 'destroyed root kept ticking'

# ---- modern 16px monospace glyphs (fonts/modern.py) ----
import text as textmod  # noqa: E402

assert textmod.char_width('A') == 9, textmod.char_width('A')
assert textmod.text_width('AB') == 18, textmod.text_width('AB')
assert textmod.char_width('中') == 16, textmod.char_width('中')  # CJK stays 16
adv = textmod.draw_char(fb, 200, 200, 'I', (255, 255, 255))
assert adv == 9, adv
# the modern 'I' paints ink inside its 16x16 cell (thin stem), and the
# pixels beyond the ink width stay the background
cell = [fb.pixel(200 + c, 200 + row) for row in range(16) for c in range(16)]
assert (255, 255, 255) in cell, 'glyph painted nothing in its cell'
assert fb.pixel(200 + 12, 200) == (22, 26, 34), fb.pixel(200 + 12, 200)

# ---- display orientation (MIRROR_X) ----
# VirtualBox's VMSVGA renders the VBE framebuffer left-to-right (verified
# pixel-exact: the pre-compensation screenshot and the compensated one are
# horizontal mirrors of each other), so the gate runs with MIRROR_X off:
# logical (0, 0) lands at raw physical column 0 and fill_rect writes its
# x-range in place.
assert fb.mirror_x is False, 'host gate should run with MIRROR_X off'
fb.set_pixel(0, 0, 255, 0, 0)
o0 = 0 * fb.bpp
assert fb.mem[o0] == 0 and fb.mem[o0 + 1] == 0 and fb.mem[o0 + 2] == 255, \
    'logical (0,0) not written to physical column 0'
assert fb.pixel(0, 0) == (255, 0, 0), 'readback disagrees with the write'
# fill_rect writes logical [10, 13) at physical [10, 13); the pixels
# outside that range stay untouched
fb.fill_rect(10, 5, 3, 1, 1, 2, 3)
p_lo = (5 * fb.stride + 10) * fb.bpp   # physical col of logical 10
p_hi = (5 * fb.stride + 12) * fb.bpp   # physical col of logical 12
for p in range(p_lo, p_hi + fb.bpp, fb.bpp):
    assert fb.mem[p + 2] == 1 and fb.mem[p + 1] == 2 and fb.mem[p] == 3
out = (5 * fb.stride + 13) * fb.bpp
assert fb.mem[out] != 3 or fb.mem[out + 2] != 1, 'rect leaked right'
assert fb.pixel(10, 5) == (1, 2, 3) and fb.pixel(12, 5) == (1, 2, 3)

# ---- embedded: WidgetWindow inside wm.Desktop (draw_content hook) ----
import wm  # noqa: E402

eroot = tk.Tk(title='embedded')
ebtn_calls = []
tk.Label(eroot, text='in window', fg=(150, 200, 150)).pack()
ebtn = tk.Button(eroot, text='Press me', command=lambda: ebtn_calls.append('pressed'))
ebtn.pack()
ecv = tk.Canvas(eroot, width=80, height=40)
ecv.create_line(0, 0, 60, 30, color=(255, 160, 80))
ecv.pack()

ewin = tk.WidgetWindow('win.widgets', 20, 30, 180, 110, eroot)
desk = wm.Desktop(fb, kb, [ewin], focus=0, lang='en')

# the button is the only focusable -> focused by default inside the window
assert ebtn.focused is True
# Enter routed through wm's on_char activates it
assert ewin.on_char('\n') is True
assert ebtn_calls == ['pressed'], ebtn_calls
assert any('tk: button "Press me" clicked' in s for s in log), log
# canvas line inside the window body: window at (20,30), title 16px, body
# origin (20,48); canvas packed at (8, 107) -> line midpoint abs (58, 122)
assert fb.pixel(58, 122) == (255, 160, 80), (fb.pixel(58, 122))

print('TK-TEST: all assertions passed (%d log lines, %d timers)' % (len(log), len(kern.timers)))
