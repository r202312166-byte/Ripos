"""text.py -- glyph text renderer for the Ripos framebuffer (pure Python).

ASCII glyphs come from fonts/modern.py: a 16px monospace atlas rasterized
from Consolas at build time (thinner, smoother strokes than the old 2x-
scaled 8x8 font, and it uses a fast row-slice paint path).  CJK glyphs
are 16x16 from GNU Unifont (fonts/unicode.py).  Lines are 16px tall, so
mixed English/Chinese text aligns.

Text is drawn with a read-modify-write of each glyph row: only the SET bits
are painted, so the background (windows, title bars) is preserved instead of
being zeroed by a plain slice write.
"""

import fonts.modern as modern
import fonts.unicode as unicode

LINE_H = 16
CJK_W = 16
CELL_W = modern.CELL_W   # 16-bit glyph row width (matches CJK)

# Compatibility: the old 8x8 ASCII font was rendered SCALE x SCALE.  The
# modern atlas is native 16px, so ASCII is always drawn at scale 1; SCALE
# is kept for callers that still pass it.
SCALE = 1
ASCII_W = 9   # modern monospace advance (per-glyph, see char_width)


def _rev(bits, width):
    """Reverse a glyph row's bits (MSB is the leftmost column).
    Used by MIRROR_GLYPHS: flips a glyph horizontally in place."""
    out = 0
    for c in range(width):
        if bits & (1 << c):
            out |= 1 << (width - 1 - c)
    return out


def _ascii_advance(cp):
    g = modern.GLYPHS.get(cp)
    return g[0] if g else ASCII_W


def char_width(ch, scale=None):
    cp = ord(ch)
    return CJK_W if cp >= 0x80 else _ascii_advance(cp)


def text_width(s, scale=None):
    return sum(char_width(c) for c in s)


def _paint_row(fb, x, y, bits, width, color, scale=1):
    """Overwrite only the set bits of a glyph row (keep the background).
    With scale > 1 each set bit paints a scale x scale block."""
    if y < 0 or y >= fb.height:
        return
    if x < 0 or x >= fb.width:
        return
    m = fb.mem
    bpp = fb.bpp
    r, g, b = color
    cols = [c for c in range(width) if bits & (1 << (width - 1 - c))]
    if not cols:
        return
    if scale == 1 and bpp == 3 and not getattr(fb, 'mirror_x', False):
        # fast path: read-modify-write one row slice (stride is pixels);
        # only valid when the display does not mirror the framebuffer
        w = width
        if x + w > fb.width:
            w = fb.width - x
        o = fb._off(x, y)                 # physical offset (viewport origin)
        row = bytearray(m[o:o + w * 3])  # read the current background
        for col in range(w):
            if bits & (1 << (width - 1 - col)):
                p = col * 3
                row[p] = b
                row[p + 1] = g
                row[p + 2] = r
        m[o:o + w * 3] = bytes(row)
        return
    # generic: paint each set bit as a scale x scale block (any bpp/scale).
    # Offsets go through fb._off so mirrored displays write to the mirrored
    # column (the block order is reversed in memory, which the display
    # un-mirrors back).
    for col in cols:
        xx = x + col * scale
        for dy in range(scale):
            yy = y + dy
            if yy < 0 or yy >= fb.height:
                continue
            if bpp == 2:
                v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                for dx in range(scale):
                    if xx + dx < fb.width:
                        o = fb._off(xx + dx, yy)
                        m[o] = v & 0xFF
                        m[o + 1] = (v >> 8) & 0xFF
            elif bpp >= 3:
                for dx in range(scale):
                    if xx + dx < fb.width:
                        o = fb._off(xx + dx, yy)
                        m[o] = b
                        m[o + 1] = g
                        m[o + 2] = r
            else:  # 1 byte grayscale
                v = (r + g + b) // 3
                for dx in range(scale):
                    if xx + dx < fb.width:
                        m[fb._off(xx + dx, yy)] = v


# ---------------------------------------------------------------------------
# Precomputed ink: for each glyph, the rows that carry ink and, per row, the
# set column indices (0-based within the ink width).  The paint path then
# skips the per-row bit scan and the per-row function call.
# ---------------------------------------------------------------------------

_INK = {}   # cp -> (normal [(row, cols)], flipped [(row, cols)])


def _build_ink():
    for cp in range(32, 127):
        g = modern.GLYPHS.get(cp)
        if g is None:
            continue
        _, ink_w, rows = g
        norm = []
        flip = []
        for row in range(modern.LINE_H):
            bits = rows[row]
            if not bits:
                continue
            cols = tuple(c for c in range(ink_w)
                         if bits & (1 << (modern.CELL_W - 1 - c)))
            if not cols:
                continue
            norm.append((row, cols))
            flip.append((row, tuple(ink_w - 1 - c for c in cols)))
        if norm:
            _INK[cp] = (norm, flip)
    # CJK glyphs (16x16, full-width) get the same treatment
    for cp, data in unicode.GLYPHS.items():
        if cp < 0x80:
            continue
        norm = []
        flip = []
        for row in range(16):
            bits = (data[row * 2] << 8) | data[row * 2 + 1]
            if not bits:
                continue
            cols = tuple(c for c in range(16)
                         if bits & (1 << (15 - c)))
            norm.append((row, cols))
            flip.append((row, tuple(15 - c for c in cols)))
        if norm:
            _INK[cp] = (norm, flip)


_build_ink()


def _paint_ink(fb, x, y, ink, color, width, flip):
    """Fast row-slice paint for the precomputed ink columns (bpp==3,
    non-mirrored displays).  'ink' is the [(row, cols)] list to use; rows
    that fall off the framebuffer fall back to the generic painter."""
    m = fb.mem
    if fb.bpp != 3 or getattr(fb, 'mirror_x', False):
        for row, cols in ink:
            bits = 0
            for c in cols:
                bits |= 1 << (width - 1 - c)
            _paint_row(fb, x, y + row, bits, width, color, 1)
        return
    r, g, b = color
    ox = getattr(fb, '_origin_x', 0)
    oy = getattr(fb, '_origin_y', 0)
    stride_b = fb.stride * fb.bpp
    w3 = width * 3
    base = ((y + oy) * fb.stride + (x + ox)) * fb.bpp
    fb_w = fb.width
    for row, cols in ink:
        yy = y + row
        if yy < 0 or yy >= fb.height or x < 0 or x + width > fb_w:
            # partial row: fall back to the generic per-pixel path
            bits = 0
            for c in cols:
                bits |= 1 << (width - 1 - c)
            _paint_row(fb, x, yy, bits, width, color, 1)
            continue
        o = base + row * stride_b
        seg = m[o:o + w3]
        out = bytearray(seg)
        for c in cols:
            p = c * 3
            out[p] = b
            out[p + 1] = g
            out[p + 2] = r
        m[o:o + w3] = out


def draw_char(fb, x, y, ch, color, scale=None):
    cp = ord(ch)
    flip = getattr(fb, 'mirror_glyphs', False)
    ink = _INK.get(cp)
    if ink is not None:
        norm, fl = ink
        rows = fl if flip else norm
        if cp < 0x80:
            g = modern.GLYPHS[cp]
            _paint_ink(fb, x, y, rows, color, g[1] or 1, flip)
            return g[0]
        _paint_ink(fb, x, y, rows, color, CJK_W, flip)
        return CJK_W
    # glyph without a precomputed ink entry: generic path
    if cp < 0x80:
        g = modern.GLYPHS.get(cp)
        if g is None:
            return ASCII_W
        adv, ink_w, glyph_rows = g
        for row in range(LINE_H):
            bits = glyph_rows[row]
            if not bits:
                continue
            if flip:
                bits = _rev(bits, CELL_W)
            _paint_row(fb, x, y + row, bits, ink_w or 1, color, 1)
        return adv
    data = unicode.GLYPHS.get(cp)
    if data is None:
        return CJK_W
    for row in range(16):
        bits = (data[row * 2] << 8) | data[row * 2 + 1]
        if flip:
            bits = _rev(bits, CJK_W)
        if bits:
            _paint_row(fb, x, y + row, bits, CJK_W, color, 1)
    return CJK_W


def draw_text(fb, x, y, s, color, max_width=None, scale=None):
    if scale is None:
        scale = SCALE
    cx = x
    for ch in s:
        w = char_width(ch, scale)
        if max_width is not None and (cx - x) + w > max_width:
            break
        cx += draw_char(fb, cx, y, ch, color, scale)
    return cx
