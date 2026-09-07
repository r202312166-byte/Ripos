# mouse-test.py -- host gate for the M9 mouse driver (no OS boot).
# Stubs kern, imports the initramfs mouse driver + framebuffer, feeds
# raw packets like the kernel would, and asserts position clamping,
# click/double-click synthesis and cursor compositing (the sprite
# restores the pixels underneath when it moves).

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
        return None   # no hardware mode switch on the host

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

import mouse
from framebuffer import Framebuffer


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)

fb = Framebuffer()
ms = mouse.Mouse(fb)

# INVERT_Y: the driver flips the vertical delta to compensate hosts whose
# PS/2 packets report y inverted (see mouse.py).  So a raw packet with
# dy=+20 ("down" per the PS/2 spec) moves the cursor UP by 20.
check(mouse.INVERT_Y, "INVERT_Y default changed")

# position accumulation + clamping (y flipped by INVERT_Y)
ms.inject(30, 20)
check((ms.x, ms.y) == (30, 0), "move failed: %r" % ((ms.x, ms.y),))
ms.inject(-10, 5)
check((ms.x, ms.y) == (20, 0), "delta failed: %r" % ((ms.x, ms.y),))
ms.inject(-10000, -10000)
check((ms.x, ms.y) == (0, fb.height - 1), "clamp x/y failed: %r"
      % ((ms.x, ms.y),))
ms.inject(100000, 100000)
check((ms.x, ms.y) == (fb.width - 1, 0),
      "clamp x/y failed: %r" % ((ms.x, ms.y),))
# drain the move events emitted by the position tests above
ms.poll()

# click synthesis: press + release (7-field events: x, y, button,
# pressed, dbl, clicks, wheel)
ms.inject(0, 0, 1)
ms.inject(0, 0, 0)
evs = ms.poll()
check(len(evs) == 2, "expected press+release, got %r" % (evs,))
check(evs[0] == (fb.width - 1, 0, 1, 1, 0, 1, 0), evs[0])
check(evs[1][3] == 0, evs[1])

# move away so the next click is not mistaken for a double click of this one
ms.inject(-200, -200)   # x left 200, y UP 200 (dy=-200 flips to +200)
check((ms.x, ms.y) == (439, 200), "move failed: %r" % ((ms.x, ms.y),))

# double click (same spot, fast): second press is dbl=1, clicks=2
ms.inject(0, 0, 1)
ms.inject(0, 0, 0)
ms.inject(0, 0, 1)
ms.inject(0, 0, 0)
evs = ms.poll()
check(len(evs) == 5 and evs[3] == (439, 200, 1, 1, 1, 2, 0),
      "dblclick not detected: %r" % (evs,))

# triple click: third press is dbl=1, clicks=3
ms.inject(0, 0, 1)
ms.inject(0, 0, 0)
ms.inject(0, 0, 1)
ms.inject(0, 0, 0)
ms.inject(0, 0, 1)
ms.inject(0, 0, 0)
evs = ms.poll()
check(evs[4] == (439, 200, 1, 1, 1, 3, 0), "triple click not detected: %r" % (evs,))

# right button also synthesizes (button 2)
ms.inject(0, 0, 2)
ms.inject(0, 0, 0)
evs = ms.poll()
check(evs[0][2] == 2, evs)

# mouse wheel: 4-field kernel packet -> (x, y, 0, 0, 0, 0, wheel)
ms.inject(0, 0, 0, 1)
evs = ms.poll()
check(len(evs) == 1 and evs[0] == (439, 200, 0, 0, 0, 0, 1), evs)
ms.inject(0, 0, 0, -2)
evs = ms.poll()
check(evs[0][6] == -2, evs)
# old 3-field kernel packets (no wheel) still decode
ms.inject(3, 0)
evs = ms.poll()
check(evs[0] == (442, 200, 0, 0, 0, 0, 0), evs)

# cursor compositing: sprite drawn, then restored on move -- INCLUDING the
# black outline pixels one pixel outside the 12x16 glyph (the afterimage /
# black-line bug: the capture must cover the full painted extent)
fb.fill_rect(100, 100, 40, 40, 0, 0, 255)   # blue backdrop
ms.x, ms.y = 110, 110
ms.draw_cursor(fb)
px = fb.pixel(110, 110)
check(px != (0, 0, 255), "cursor not drawn: %r" % (px,))
outline_px = fb.pixel(109, 110)   # one pixel left of the glyph (outline)
check(outline_px != (0, 0, 255), "outline not drawn: %r" % (outline_px,))
ms.inject(-100, 0)   # move the mouse away
px = fb.pixel(110, 110)
check(px == (0, 0, 255), "cursor did not restore the backdrop: %r" % (px,))
check(fb.pixel(109, 110) == (0, 0, 255),
      "outline pixel not restored (afterimage): %r" % (fb.pixel(109, 110),))

# the mouse handler fan-out receives events
got = []
ms.on_event(lambda ev: got.append(ev))
ms.inject(1, 1, 1)
check(len(got) == 2 and got[1] == (11, 109, 1, 1, 0, 1, 0), got)

# handlers registered via kern.on_mouse: the kernel calls ms._dispatch
check(kern.mouse_cb is not None, "kern.on_mouse not registered")
kern.mouse_cb((5, 5, 0))
check((ms.x, ms.y) == (16, 104), "kernel path failed: %r" % ((ms.x, ms.y),))

print("MOUSE-TEST: all assertions passed (%d events)" % len(ms.events))

# click-afterimage regression: a CLICK triggers a full redraw (which wipes
# the cursor) then draws the cursor again; the mouse driver's own trailing
# move() must NOT re-capture the freshly painted cursor into its saved
# buffer -- otherwise the next move restores a cursor ghost at the spot.
fb.fill_rect(0, 0, fb.width, fb.height, 10, 20, 30)   # uniform backdrop
ms.x, ms.y = 300, 200
ms.draw_cursor(fb)
# simulate the click's redraw: wipe the screen, then draw the cursor
fb.fill_rect(0, 0, fb.width, fb.height, 10, 20, 30)
ms.draw_cursor(fb)
ms.inject(0, 0, 1)          # press at the same spot (dx=dy=0)
ms.inject(0, 0, 0)          # release
ms.inject(-150, 0)          # move away
ghost = [c for c in (fb.pixel(299 + dx, 199 + dy) for dx in range(-1, 14)
                     for dy in range(-1, 18)) if c != (10, 20, 30)]
check(not ghost, "click afterimage: cursor ghost left at the click spot: %r"
      % ghost[:3])
