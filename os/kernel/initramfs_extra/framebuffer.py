"""framebuffer.py -- M6 framebuffer driver (pure Python).

The kernel publishes the mapped framebuffer to Python as a zero-copy
writable memoryview (kern.fb_mem) plus its geometry (kern.fb_info).
Everything else -- pixel formats, primitives, layout -- lives here in
Python: the only binary component of this OS is the interpreter, so the
framebuffer driver is a Python driver.

Format codes (shared with the kernel): 0 = Bgr, 1 = Rgb, 2 = U8, 3 = Unknown.
"""

import kern

# Display orientation.  VirtualBox's VMSVGA adapter renders the VBE
# linear framebuffer left-to-right (verified pixel-exact on 2026-08-17: a
# pre-compensation screenshot and the compensated one are horizontal
# mirrors of each other, so the display does NOT mirror memory).  MIRROR_X
# compensates the x-axis at the byte-offset level for the (hypothetical)
# displays that render column width-1-x: the guest keeps normal logical
# coordinates and only the writes land mirrored in memory.  Keep False
# unless you are on a display that provably mirrors the framebuffer.
MIRROR_X = False

# Some display paths (observed: VirtualBox VMSVGA in a scaled window)
# render each glyph cell horizontally flipped while keeping text position
# left-to-right -- glyphs read correctly in the right order but every
# letter is inverted ('p' looks like 'q').  MIRROR_GLYPHS compensates by
# flipping each glyph bitmap horizontally at paint time; positions are
# unchanged.  Keep False unless the screen shows inverted letters.
MIRROR_GLYPHS = False


class Framebuffer:
    """A drawing surface over the kernel framebuffer."""

    def __init__(self):
        info = kern.fb_info()
        if info is None:
            raise RuntimeError('no framebuffer available')
        w, h, stride, bpp, fmt = info
        mem = kern.fb_mem()
        if mem is None:
            raise RuntimeError('no framebuffer memory')
        self.width = w
        self.height = h
        # stride is in PIXELS (bootloader semantics), not bytes; byte
        # offsets are computed as (y * stride + x) * bpp
        self.stride = stride
        self.bpp = bpp
        self.format = fmt
        self.mem = mem
        self.mirror_x = MIRROR_X
        self.mirror_glyphs = MIRROR_GLYPHS
        # channel byte offsets inside one pixel, in (r, g, b) order
        self._ch = (2, 1, 0) if fmt == 0 else (0, 1, 2)
        # Physical framebuffer geometry (the virtual viewport can be
        # smaller, centered via _origin_*; see set_resolution).
        self._phys_w = w
        self._phys_h = h
        self._origin_x = 0
        self._origin_y = 0

    # -- low level ------------------------------------------------------

    def _off(self, x, y):
        if self.mirror_x:
            x = self.width - 1 - x
        return ((y + self._origin_y) * self.stride
                + (x + self._origin_x)) * self.bpp

    def set_pixel(self, x, y, r, g, b):
        if 0 <= x < self.width and 0 <= y < self.height:
            o = self._off(x, y)
            m = self.mem
            if self.bpp == 3 or self.bpp == 4:
                m[o + self._ch[0]] = r & 0xFF
                m[o + self._ch[1]] = g & 0xFF
                m[o + self._ch[2]] = b & 0xFF
            elif self.bpp == 2:
                v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                m[o] = v & 0xFF
                m[o + 1] = (v >> 8) & 0xFF
            else:  # 1 byte grayscale
                m[o] = (r + g + b) // 3

    def pixel(self, x, y):
        """Read back the pixel at (x, y) as an (r, g, b) tuple or None."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        o = self._off(x, y)
        m = self.mem
        if self.bpp == 2:
            v = m[o] | (m[o + 1] << 8)
            return ((v >> 11) & 0x1F) << 3, ((v >> 5) & 0x3F) << 2, (v & 0x1F) << 3
        r = m[o + self._ch[0]]
        g = m[o + self._ch[1]]
        b = m[o + self._ch[2]]
        return r, g, b

    # -- primitives -----------------------------------------------------

    def _row_pattern(self, r, g, b):
        r, g, b = r & 0xFF, g & 0xFF, b & 0xFF
        if self.bpp == 3:
            return bytes((b, g, r))
        if self.bpp == 4:
            return bytes((b, g, r, 0xFF))
        if self.bpp == 2:
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            return bytes((v & 0xFF, (v >> 8) & 0xFF))
        return bytes(((r + g + b) // 3,))

    def _rev_pixels(self, pat, n):
        """Reverse a row pattern in pixel-sized chunks (mirrored rows)."""
        bpp = self.bpp
        return b''.join(pat[i:i + bpp] for i in range((n - 1) * bpp, -1, -bpp))

    def fill_rect(self, x, y, w, h, r, g, b):
        if w <= 0 or h <= 0:
            return
        if self.mirror_x:
            # display rows read right-to-left: guest [x, x+w) lands at
            # physical [width-x-w, width-x), pixel order reversed
            x = self.width - x - w
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, self.width), min(y + h, self.height)
        if x0 >= x1 or y0 >= y1:
            return
        pat = self._row_pattern(r, g, b) * (x1 - x0)
        if self.mirror_x:
            pat = self._rev_pixels(pat, x1 - x0)
        m = self.mem
        # raw byte offset of the first physical pixel of the row; note this
        # must NOT go through _off() when mirror_x is set -- _off applies the
        # mirror again (correct for single pixels), but here x0 already IS the
        # physical column (width-x-w) and re-mirroring would push the write
        # past the end of the row.
        base = (self._origin_y * self.stride + self._origin_x) * self.bpp
        for yy in range(y0, y1):
            o = base + yy * self.stride * self.bpp + x0 * self.bpp
            m[o:o + len(pat)] = pat

    def draw_rect(self, x, y, w, h, r, g, b):
        self.fill_rect(x, y, w, 1, r, g, b)
        self.fill_rect(x, y + h - 1, w, 1, r, g, b)
        self.fill_rect(x, y, 1, h, r, g, b)
        self.fill_rect(x + w - 1, y, 1, h, r, g, b)

    def line(self, x0, y0, x1, y1, r, g, b):
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set_pixel(x0, y0, r, g, b)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def clear(self, r=0, g=0, b=0):
        self.fill_rect(0, 0, self.width, self.height, r, g, b)

    # ---- resolution ----------------------------------------------------

    def set_resolution(self, w, h):
        """Change the display resolution.

        First tries a real kernel mode-set (Bochs VBE -- QEMU stdvga).  When
        the hardware has no runtime mode switch (VirtualBox VMSVGA) it falls
        back to a virtual resolution: a centered viewport of w x h over the
        physical screen (the UI then lays out to the new size, everything
        outside the viewport stays black).

        Returns (w, h, real) where real is True when the physical display
        actually changed mode.
        """
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return (self.width, self.height, False)
        new = kern.fb_set_mode(w, h)
        if new is not None:
            # Real mode change: the kernel reprogrammed the display.  This
            # runs FIRST so modes larger than the boot framebuffer (e.g.
            # res 1920x1080 on QEMU, where the kernel mapped extra video
            # memory) work even though the physical boot size is smaller.
            w, h, stride, bpp, fmt = new
            self.width, self.height = w, h
            self.stride = stride
            self.bpp = bpp
            self.format = fmt
            self._ch = (2, 1, 0) if fmt == 0 else (0, 1, 2)
            self._origin_x = 0
            self._origin_y = 0
            m = kern.fb_mem()
            if m is not None:
                self.mem = m
            return (w, h, True)
        # No runtime hardware mode switch: the virtual viewport cannot
        # exceed the physical framebuffer (the VBE memory is only as big
        # as the boot mode).
        if w > self._phys_w or h > self._phys_h:
            return (self.width, self.height, False)
        # Virtual resolution: centered viewport over the physical screen.
        self.width, self.height = w, h
        self._origin_x = (self._phys_w - w) // 2
        self._origin_y = (self._phys_h - h) // 2
        # Blank the whole physical screen (letterbox margins + viewport);
        # row-by-row so we never allocate a full-screen buffer.
        row = self.stride * self.bpp
        zero_row = b'\x00' * row
        m = self.mem
        for yy in range(self._phys_h):
            m[yy * row:(yy + 1) * row] = zero_row
        return (w, h, False)
