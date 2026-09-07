"""mouse.py -- M9 mouse driver (pure Python).

The kernel decodes PS/2 mouse packets and hands (dx, dy, buttons) -- or
(dx, dy, buttons, wheel) once the wheel-enabled kernel is used -- to one
registered callback (kern.on_mouse).  This driver turns that into a real
driver: it accumulates an absolute cursor position (clamped to the
screen), tracks button state to synthesize press/release/double/triple
click events, fans events out to any number of Python handlers, and
renders a composited cursor sprite on the framebuffer.

Event tuples delivered to handlers: (x, y, button, pressed, dbl, clicks, wheel)
  x, y    -- absolute cursor position in pixels (screen coordinates)
  button  -- 1 = left, 2 = right, 4 = middle; 0 = move / wheel
  pressed -- 1 on press, 0 on release / move / wheel
  dbl     -- 1 if this press is a double- or triple-click (left only)
  clicks  -- 1/2/3: click count of this press (single/double/triple)
  wheel   -- signed wheel delta for a scroll event (button 0, pressed 0)
"""

import kern

# Some host mouse paths (observed on VirtualBox VMSVGA guests) report the
# PS/2 y deltas inverted -- moving the physical mouse DOWN arrives as a
# NEGATIVE dy, so the cursor runs opposite to the hand while horizontal
# stays correct.  INVERT_Y compensates by flipping dy in the driver.  Set
# it to False on hosts that follow the PS/2 convention (positive dy =
# down), e.g. plain QEMU.
INVERT_Y = True


# 12x16 arrow cursor, rows of 12 bits (MSB = leftmost column)
CURSOR_W = 12
CURSOR_H = 16
CURSOR_BITS = [
    0b100000000000,
    0b110000000000,
    0b111000000000,
    0b111100000000,
    0b111110000000,
    0b111111000000,
    0b111111100000,
    0b111111110000,
    0b111111111000,
    0b111111111100,
    0b111111111110,
    0b111111111111,
    0b111111110000,
    0b111101110000,
    0b111100111000,
    0b111000011000,
]
CURSOR_FG = (255, 255, 255)
CURSOR_BG = (10, 10, 10)

DEFAULT = None   # the most recently created Mouse (set in Mouse.__init__)

DBL_MS = 400
DBL_PX = 8


class Cursor:
    """A composited cursor sprite over the framebuffer.

    Captures the pixels under the sprite, draws it, and restores the
    captured pixels when it moves -- so it never damages the widgets
    underneath.  The capture covers the WHOLE painted extent (the black
    outline is drawn one pixel outside the 12x16 glyph), so restoring
    leaves no black outline residue behind the arrow.

    draw() is the "after a full screen redraw" entry: it discards any
    stale capture and re-captures.  move() is the live entry (restore
    the old spot, capture + draw at the new spot).
    """

    def __init__(self):
        self.x = 0
        self.y = 0
        self._saved = None   # (rect (x0,y0,w,h), bytes)
        self._at = None      # last drawn position (x, y)
        self._fb = None

    def reset(self):
        """Drop the saved region (e.g. after a resolution change)."""
        self._saved = None
        self._at = None

    def _clamp(self, fb, x, y):
        x = max(0, min(fb.width - CURSOR_W, x))
        y = max(0, min(fb.height - CURSOR_H, y))
        return x, y

    def _paint_rect(self, x, y):
        # the full area the sprite + its ±1 outline can touch
        return (x - 1, y - 1, CURSOR_W + 2, CURSOR_H + 2)

    def _capture(self, fb, x, y):
        rx, ry, rw, rh = self._paint_rect(x, y)
        x0 = max(0, rx)
        y0 = max(0, ry)
        x1 = min(fb.width, rx + rw)
        y1 = min(fb.height, ry + rh)
        data = bytearray()
        for yy in range(y0, y1):
            o = fb._off(x0, yy)
            data += bytes(fb.mem[o:o + (x1 - x0) * fb.bpp])
        return (x0, y0, x1 - x0, y1 - y0), bytes(data)

    def _paste(self, fb, rect, data):
        x0, y0, w, h = rect
        bpp = fb.bpp
        off = 0
        for yy in range(y0, y0 + h):
            o = fb._off(x0, yy)
            fb.mem[o:o + w * bpp] = data[off:off + w * bpp]
            off += w * bpp

    def _paint(self, fb, x, y):
        # black outline (sprite shifted by one pixel in 4 directions)
        for (ox, oy) in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            self._paint_bits(fb, x + ox, y + oy, CURSOR_BG)
        self._paint_bits(fb, x, y, CURSOR_FG)

    def _paint_bits(self, fb, x, y, color):
        bpp = fb.bpp
        for row, bits in enumerate(CURSOR_BITS):
            yy = y + row
            if yy < 0 or yy >= fb.height:
                continue
            for col in range(CURSOR_W):
                if bits & (1 << (CURSOR_W - 1 - col)):
                    xx = x + col
                    if 0 <= xx < fb.width:
                        o = fb._off(xx, yy)
                        if bpp == 3 or bpp == 4:
                            fb.mem[o] = color[2]
                            fb.mem[o + 1] = color[1]
                            fb.mem[o + 2] = color[0]
                        elif bpp == 2:
                            v = ((color[0] & 0xF8) << 8) | ((color[1] & 0xFC) << 3) | (color[2] >> 3)
                            fb.mem[o] = v & 0xFF
                            fb.mem[o + 1] = (v >> 8) & 0xFF
                        else:
                            fb.mem[o] = (color[0] + color[1] + color[2]) // 3

    def draw(self, fb, x, y):
        """After a full redraw: the old capture is stale, re-capture."""
        x, y = self._clamp(fb, x, y)
        self._saved = self._capture(fb, x, y)
        self._paint(fb, x, y)
        self._fb = fb
        self._at = (x, y)
        self.x, self.y = x, y

    def restore(self, fb):
        """Remove the currently painted cursor (paste the saved pixels).
        Used before PARTIAL repaints that do not cover the cursor, so the
        next capture cannot contain the cursor itself (an afterimage).
        """
        if self._saved is not None and self._fb is fb:
            rect, data = self._saved
            self._paste(fb, rect, data)

    def move(self, fb, x, y):
        """Live move: restore the old spot, then capture + draw.  When the
        position did not change (e.g. a button-only packet right after a
        redraw already drew the cursor), do NOT re-draw: re-capturing at
        the same spot would store the freshly painted cursor itself and
        leave an afterimage on the next move.
        """
        if self._saved is not None and self._fb is fb:
            rect, data = self._saved
            if rect[0] != x - 1 or rect[1] != y - 1:
                self._paste(fb, rect, data)
        if self._at != (x, y) or self._saved is None or self._fb is not fb:
            self.draw(fb, x, y)
        else:
            self.x, self.y = x, y


class Mouse:
    """Absolute-position mouse driver over kern.on_mouse packets."""

    def __init__(self, fb=None):
        global DEFAULT
        self.handlers = []
        self.events = []      # every synthesized event, for poll()/tests
        self.x = 0
        self.y = 0
        self.buttons = 0
        self.fb = fb
        self.cursor = Cursor()
        self._last_press = None   # (tick, x, y, clicks) of the last left press
        kern.on_mouse(self._dispatch)
        DEFAULT = self

    def on_event(self, handler):
        """Register a callback; it receives (x, y, button, pressed, dbl,
        clicks, wheel)."""
        self.handlers.append(handler)

    def set_fb(self, fb):
        self.fb = fb
        self.cursor.reset()
        self._clamp()

    def _clamp(self):
        if self.fb is None:
            return
        self.x = max(0, min(self.fb.width - 1, self.x))
        self.y = max(0, min(self.fb.height - 1, self.y))

    def _emit(self, ev):
        self.events.append(ev)
        for h in self.handlers:
            h(ev)

    def _dispatch(self, ev):
        """One kernel packet: (dx, dy, buttons) or (dx, dy, buttons, wheel)."""
        if len(ev) >= 4:
            dx, dy, buttons, wheel = ev
        else:
            dx, dy, buttons = ev
            wheel = 0
        moved = False
        if self.fb is not None:
            self.x += dx
            self.y += (-dy if INVERT_Y else dy)
            self._clamp()
        if wheel:
            # scroll event: (x, y, 0, 0, 0, 0, wheel) -- the wheel delta
            self._emit((self.x, self.y, 0, 0, 0, 0, wheel))
            moved = True
        if dx or dy:
            # move event: (x, y, 0, 0, 0, 0, 0) -- useful for hover/hit-tests
            # (the tk routing ignores button 0, so this is harmless there)
            self._emit((self.x, self.y, 0, 0, 0, 0, 0))
            moved = True
        old = self.buttons
        self.buttons = buttons
        for b in (1, 2, 4):
            pressed = bool(buttons & b)
            was = bool(old & b)
            if pressed and not was:
                dbl = 0
                clicks = 1
                now = kern.tick()
                if b == 1 and self._last_press is not None:
                    lt, lx, ly, lc = self._last_press
                    if (now - lt < DBL_MS
                            and abs(self.x - lx) < DBL_PX
                            and abs(self.y - ly) < DBL_PX):
                        clicks = min(lc + 1, 3)
                        dbl = 1 if clicks >= 2 else 0
                    else:
                        self._last_press = None
                if b == 1:
                    self._last_press = (now, self.x, self.y, clicks)
                else:
                    self._last_press = None
                self._emit((self.x, self.y, b, 1, dbl, clicks, 0))
            elif not pressed and was:
                self._emit((self.x, self.y, b, 0, 0, 0, 0))
        if self.fb is not None:
            self.cursor.move(self.fb, self.x, self.y)

    def draw_cursor(self, fb):
        """Call after every full screen redraw so the cursor is on top."""
        if fb is not None:
            self.cursor.draw(fb, self.x, self.y)

    def clear_cursor(self, fb):
        """Remove the painted cursor (before a partial repaint that does
        not cover it, so the next capture stays clean)."""
        if fb is not None:
            self.cursor.restore(fb)

    def poll(self):
        """Return and clear all pending synthesized events."""
        q = self.events
        self.events = []
        return q

    def inject(self, dx, dy, buttons=0, wheel=0):
        """Host-test helper: feed a raw packet like the kernel would."""
        self._dispatch((dx, dy, buttons, wheel))
