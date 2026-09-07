# png.py -- pure-Python PNG codec for Ripos (M9.2).
#
# Decoder: signature + chunk walk, IHDR/PLTE/tRNS/IDAT/IEND; zlib-inflates
# the IDAT stream and unfilters every scanline (None/Sub/Up/Average/Paeth).
# Supports bit depths 1/2/4/8/16 and colour types 0 (gray), 2 (RGB),
# 3 (palette), 4 (gray+alpha), 6 (RGBA); non-interlaced only.
# Encoder: writes 8-bit RGB/RGBA/gray PNG with Sub filtering + zlib.
# Depends only on the stdlib zlib module (M9.1).

import struct
import zlib


class PngError(Exception):
    pass


class Image:
    """A simple RGB(A) image: w, h, and a flat bytearray of pixels.
    mode is 'rgb' (3 bytes/px) or 'rgba' (4 bytes/px)."""

    __slots__ = ("w", "h", "mode", "pixels", "_has_alpha_probe")

    def __init__(self, w, h, mode="rgb"):
        self.w = w
        self.h = h
        self.mode = mode
        self.pixels = bytearray(w * h * (4 if mode == "rgba" else 3))

    def _n(self):
        return 4 if self.mode == "rgba" else 3

    def get(self, x, y):
        n = self._n()
        i = (y * self.w + x) * n
        return tuple(self.pixels[i : i + n])

    def set(self, x, y, rgb):
        n = self._n()
        i = (y * self.w + x) * n
        if n == 4 and len(rgb) == 3:
            self.pixels[i : i + 4] = bytes((rgb[0], rgb[1], rgb[2], 255))
        else:
            self.pixels[i : i + n] = bytes(rgb[:n])

    def composite(self, bg=(0, 0, 0), checker=None):
        """Return an opaque RGB Image: alpha-blend this image over a
        background.  bg is a solid (r, g, b).  checker=(c1, c2, cell) draws
        a checkerboard (standard "transparent" background) so see-through
        areas are visible."""
        n = self._n()
        w, h = self.w, self.h
        if n == 3 and checker is None:
            out = Image(w, h, "rgb")
            out.pixels[:] = self.pixels
            return out
        if checker is not None:
            c1, c2, cell = checker
            c1 = (c1[0], c1[1], c1[2])
            c2 = (c2[0], c2[1], c2[2])
        else:
            c1 = c2 = (bg[0], bg[1], bg[2])
            cell = 1
        px = self.pixels
        out = Image(w, h, "rgb")
        op = out.pixels
        i2 = 0
        p = 0
        for y in range(h):
            cy = (y // cell) & 1 if checker is not None else 0
            for x in range(w):
                r, g, b = px[p], px[p + 1], px[p + 2]
                if n == 4:
                    a = px[p + 3]
                else:
                    a = 255
                p += n
                if a == 255:
                    op[i2] = r
                    op[i2 + 1] = g
                    op[i2 + 2] = b
                else:
                    if checker is not None and ((x // cell) ^ cy) & 1:
                        br, bgx, bb = c1
                    else:
                        br, bgx, bb = c2
                    if a == 0:
                        op[i2] = br
                        op[i2 + 1] = bgx
                        op[i2 + 2] = bb
                    else:
                        f = a / 255.0
                        op[i2] = int(r * f + br * (1.0 - f)) & 0xFF
                        op[i2 + 1] = int(g * f + bgx * (1.0 - f)) & 0xFF
                        op[i2 + 2] = int(b * f + bb * (1.0 - f)) & 0xFF
                i2 += 3
        return out

    def draw(self, fb, x, y, scale=1):
        """Paint the image onto a Ripos framebuffer at (x, y), optionally
        scaled by an integer factor (nearest neighbour)."""
        self.blit(fb, x, y, scale)

    def _find_alpha(self):
        """True if the RGBA image has any non-opaque pixel (cached)."""
        n = self._n()
        if n != 4:
            return False
        try:
            return self._has_alpha_probe
        except AttributeError:
            a = self.pixels[3 :: 4]
            self._has_alpha_probe = (min(a) if a else 255) < 255
            return self._has_alpha_probe

    def blit(self, fb, x, y, scale=1.0, bg=None):
        """Fast blit of the image onto a Ripos framebuffer at (x, y).

        scale is a float >= 0.05: values < 1 downscale (nearest sample),
        values > 1 upscale (nearest expansion).  RGBA sources are
        alpha-composited over bg (default opaque black) unless every pixel
        is opaque.  Only the on-screen portion of the image is computed, so
        a picture far larger than the screen repaints in screen area, not
        image area.  Rows are packed into bytearrays and written with
        memoryview slice assignment so the bulk copy runs in C."""
        scale = float(scale)
        if scale < 0.05:
            return
        x = int(round(x))
        y = int(round(y))
        n = self._n()
        w0, h0 = self.w, self.h
        px = self.pixels
        mem = fb.mem
        bpp = fb.bpp
        ow = max(1, int(round(w0 * scale)))
        oh = max(1, int(round(h0 * scale)))
        x0 = max(0, x)
        x1 = min(fb.width, x + ow)
        if x1 <= x0:
            return
        y0 = max(0, y)
        y1 = min(fb.height, y + oh)
        if y1 <= y0:
            return
        src_w = w0 * n
        has_a = self._find_alpha() if n == 4 else False
        if has_a and bg is None:
            bg = (0, 0, 0)
        elif not has_a:
            bg = None
        # scale == 1 (the common, piercingly-fast path)
        if scale == 1 and bg is None and bpp in (3, 4):
            self._blit1(fb, x, y, x0, x1, y0, y1)
            return
        # integer upscale: repeat source pixels
        s = int(scale)
        if bg is None and bpp in (3, 4) and scale == s and s > 1:
            self._blit_int_up(fb, x, y, s, x0, x1, y0, y1)
            return
        # exact 1/k downscale: strided slices (C-speed)
        if bg is None and bpp in (3, 4) and scale < 1:
            k = int(round(1.0 / scale))
            if k >= 1 and abs((1.0 / k) - scale) < 1e-4:
                self._blit_stride_down(fb, x, y, k, x0, x1, y0, y1)
                return
        # all other scales (awkward fractions, RGBA-on-color): per-pixel
        self._blit_map(fb, x, y, scale, has_a, bg, x0, x1, y0, y1)

    def _blit1(self, fb, x, y, x0, x1, y0, y1):
        """scale == 1, opaque source, bpp 3/4: pure slice reorder + copy."""
        w0, h0 = self.w, self.h
        px = self.pixels
        mem = fb.mem
        n = 4 if self._n() == 4 else 3
        src_w = w0 * n
        bpp = fb.bpp
        c0 = x0 - x
        c1 = x1 - x
        fb_bgr = fb._ch[0] == 2
        for yy in range(y0, y1):
            row = (yy - y) * src_w
            if row < 0 or row >= src_w * h0:
                continue
            b0 = row + c0 * n
            b1 = row + c1 * n
            if n == 3:
                if fb_bgr:
                    out = bytearray((x1 - x0) * bpp)
                    out[0::bpp] = px[b0 + 2 : b1 + 2 : 3]
                    out[1::bpp] = px[b0 + 1 : b1 + 1 : 3]
                    out[2::bpp] = px[b0 : b1 : 3]
                    if bpp == 4:
                        out[3::4] = bytes([255]) * (x1 - x0)
                else:
                    out = bytearray(px[b0:b1])
                    if bpp == 4:
                        out = bytearray((x1 - x0) * 4)
                        out[0::4] = px[b0:b1:3]
                        out[1::4] = px[b0 + 1:b1 + 1:3]
                        out[2::4] = px[b0 + 2:b1 + 2:3]
                        out[3::4] = bytes([255]) * (x1 - x0)
            else:  # opaque RGBA source flattened to RGB
                out = bytearray((x1 - x0) * bpp)
                if fb_bgr:
                    out[0::bpp] = px[b0 + 2 : b1 + 2 : 4]
                    out[1::bpp] = px[b0 + 1 : b1 + 1 : 4]
                    out[2::bpp] = px[b0 : b1 : 4]
                else:
                    out[0::bpp] = px[b0 : b1 : 4]
                    out[1::bpp] = px[b0 + 1 : b1 + 1 : 4]
                    out[2::bpp] = px[b0 + 2 : b1 + 2 : 4]
                if bpp == 4:
                    out[3::4] = bytes([255]) * (x1 - x0)
            dst = fb._off(x0, yy)
            mem[dst : dst + len(out)] = out

    def _blit_int_up(self, fb, x, y, s, x0, x1, y0, y1):
        """Integer upscale: repeat each source pixel s x s (nearest).

        Loops over the VISIBLE source columns once per source row and
        writes each repeated block with one slice assignment; the partial
        blocks at the screen edges are clipped exactly."""
        w0, h0 = self.w, self.h
        px = self.pixels
        mem = fb.mem
        n = 4 if self._n() == 4 else 3
        src_w = w0 * n
        bpp = fb.bpp
        fb_bgr = fb._ch[0] == 2
        c0 = x0 - x
        c1 = x1 - x
        nrow = (x1 - x0) * bpp
        last_sy = None
        row = None
        for yy in range(y0, y1):
            sy = max(0, min(h0 - 1, (yy - y) // s))
            if sy != last_sy:
                last_sy = sy
                base = sy * src_w
                row = bytearray(nrow)
                j0 = max(0, c0 // s)
                j1 = min(w0, (c1 + s - 1) // s)
                for j in range(j0, j1):
                    o = base + j * n
                    blo = j * s
                    lo = c0 if blo < c0 else blo
                    hi = j * s + s
                    if hi > c1:
                        hi = c1
                    cnt = hi - lo
                    if cnt <= 0:
                        continue
                    if bpp == 4:
                        frag = bytes((px[o + 2], px[o + 1], px[o], 255)) * cnt
                    elif n == 3 and fb_bgr:
                        frag = bytes((px[o + 2], px[o + 1], px[o])) * cnt
                    elif n == 3:
                        frag = bytes(px[o : o + 3]) * cnt
                    else:  # opaque RGBA source, 3-byte fb
                        frag = bytes((px[o + 2], px[o + 1], px[o])) * cnt
                    off = (lo - c0) * bpp
                    row[off : off + cnt * bpp] = frag
            dst = fb._off(x0, yy)
            mem[dst : dst + nrow] = row

    def _blit_stride_down(self, fb, x, y, k, x0, x1, y0, y1):
        """scale == 1/k (integer k): sample every k-th source pixel via
        strided slices, so a full-screen 'fit' of a huge picture is still
        bounded by C-speed slice copies."""
        w0, h0 = self.w, self.h
        px = self.pixels
        mem = fb.mem
        n = 4 if self._n() == 4 else 3
        src_w = w0 * n
        bpp = fb.bpp
        fb_bgr = fb._ch[0] == 2
        c0 = x0 - x
        c1 = x1 - x
        ncol = c1 - c0
        step = n * k
        for yy in range(y0, y1):
            sy = max(0, min(h0 - 1, (yy - y) * k))
            base = sy * src_w
            b0 = base + c0 * step
            if n == 3 and bpp == 3:
                if fb_bgr:
                    out = bytearray(ncol * 3)
                    out[0::3] = px[b0 + 2 : b0 + ncol * step : step]
                    out[1::3] = px[b0 + 1 : b0 + 1 + ncol * step : step]
                    out[2::3] = px[b0 : b0 + ncol * step : step]
                else:
                    out = bytearray(ncol * 3)
                    out[0::3] = px[b0 : b0 + ncol * step : step]
                    out[1::3] = px[b0 + 1 : b0 + 1 + ncol * step : step]
                    out[2::3] = px[b0 + 2 : b0 + 2 + ncol * step : step]
            elif bpp == 4:
                out = bytearray(ncol * 4)
                if fb_bgr:
                    out[0::4] = px[b0 + 2 : b0 + ncol * step : step]
                    out[1::4] = px[b0 + 1 : b0 + 1 + ncol * step : step]
                    out[2::4] = px[b0 : b0 + ncol * step : step]
                else:
                    out[0::4] = px[b0 : b0 + ncol * step : step]
                    out[1::4] = px[b0 + 1 : b0 + 1 + ncol * step : step]
                    out[2::4] = px[b0 + 2 : b0 + 2 + ncol * step : step]
                out[3::4] = bytes([255]) * ncol
            else:  # opaque RGBA source, 3-byte fb
                out = bytearray(ncol * 3)
                if fb_bgr:
                    out[0::3] = px[b0 + 2 : b0 + ncol * step : step]
                    out[1::3] = px[b0 + 1 : b0 + 1 + ncol * step : step]
                    out[2::3] = px[b0 : b0 + ncol * step : step]
                else:
                    out[0::3] = px[b0 : b0 + ncol * step : step]
                    out[1::3] = px[b0 + 1 : b0 + 1 + ncol * step : step]
                    out[2::3] = px[b0 + 2 : b0 + 2 + ncol * step : step]
            dst = fb._off(x0, yy)
            mem[dst : dst + len(out)] = out

    def _blit_map(self, fb, x, y, scale, has_a, bg, x0, x1, y0, y1):
        """Arbitrary scale (incl. fractional): map each visible output cell
        to a source pixel (nearest).  Bounded by the screen, not the image,
        so huge pictures at 'fit' still repaint fast."""
        w0, h0 = self.w, self.h
        px = self.pixels
        mem = fb.mem
        bpp = fb.bpp
        n = 4 if self._n() == 4 else 3
        src_w = w0 * n
        c0 = x0 - x
        c1 = x1 - x
        inv = 1.0 / scale
        cmap = []
        for i in range(c0, c1):
            jc = int(i * inv)
            if jc < 0:
                jc = 0
            elif jc >= w0:
                jc = w0 - 1
            cmap.append(jc)
        if bpp in (3, 4):
            fb_bgr = fb._ch[0] == 2
        else:
            fb_bgr = False
        for yy in range(y0, y1):
            sy = int((yy - y) * inv)
            if sy < 0:
                sy = 0
            elif sy >= h0:
                sy = h0 - 1
            base = sy * src_w
            if bpp in (3, 4):
                dst = fb._off(x0, yy)
                if not has_a and bpp == 3:
                    if fb_bgr:
                        for c in cmap:
                            o = base + c * n
                            mem[dst] = px[o + 2]
                            mem[dst + 1] = px[o + 1]
                            mem[dst + 2] = px[o]
                            dst += 3
                    else:
                        for c in cmap:
                            o = base + c * n
                            mem[dst] = px[o]
                            mem[dst + 1] = px[o + 1]
                            mem[dst + 2] = px[o + 2]
                            dst += 3
                else:
                    # 32-bit or alpha-composited: blend then reorder
                    if has_a and bg is not None:
                        r0, g0, b0 = bg
                    else:
                        r0 = g0 = b0 = 0
                    for c in cmap:
                        o = base + c * n
                        r = px[o]
                        g = px[o + 1]
                        b = px[o + 2]
                        if has_a:
                            a = px[o + 3]
                            if a != 255:
                                f = a / 255.0
                                r = int(r * f + r0 * (1.0 - f))
                                g = int(g * f + g0 * (1.0 - f))
                                b = int(b * f + b0 * (1.0 - f))
                        if fb_bgr:
                            mem[dst] = b & 0xFF
                            mem[dst + 1] = g & 0xFF
                            mem[dst + 2] = r & 0xFF
                        else:
                            mem[dst] = r & 0xFF
                            mem[dst + 1] = g & 0xFF
                            mem[dst + 2] = b & 0xFF
                        dst += bpp
            elif bpp == 2:
                col = x0
                for c in cmap:
                    o = base + c * n
                    r, g, b = px[o], px[o + 1], px[o + 2]
                    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                    oo = fb._off(col, yy)
                    mem[oo] = v & 0xFF
                    mem[oo + 1] = (v >> 8) & 0xFF
                    col += 1
            else:  # 1-byte grayscale
                col = x0
                for c in cmap:
                    o = base + c * n
                    r, g, b = px[o], px[o + 1], px[o + 2]
                    oo = fb._off(col, yy)
                    mem[oo] = (r + g + b) // 3
                    col += 1


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------

_SIG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def load_png(data):
    """Decode PNG bytes into an Image (mode 'rgb' or 'rgba').

    Uses the kernel's C stb_image decoder (_img) when present (3-channel RGB
    -- transparent PNGs flatten over black in that path); falls back to the
    pure-Python decoder (host runner, or when alpha handling matters)."""
    if not data.startswith(_SIG):
        raise PngError("not a PNG")
    try:
        import _img
        w, h, _n, raw = _img.decode(data)
        img = Image(w, h, "rgb")
        img.pixels[:] = raw
        return img
    except ImportError:
        pass
    except Exception:
        pass
    pos = 8
    width = height = depth = ctype = None
    idat = bytearray()
    plte = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length:
            raise PngError("truncated chunk")
        if ctag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            if body[10] != 0 or body[12] != 0:
                raise PngError("unsupported compression/filter/interlace")
            if body[12] != 0:
                raise PngError("interlaced PNG not supported")
        elif ctag == b"PLTE":
            plte = body
        elif ctag == b"IDAT":
            idat += body
        elif ctag == b"IEND":
            break
        pos += 12 + length
    if width is None:
        raise PngError("no IHDR")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    # bytes per pixel on the wire (bit depth < 8 packs)
    bpp = max(1, (depth * channels) // 8) if depth >= 8 else 1
    stride = (width * depth * channels + 7) // 8

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise PngError("IDAT: %s" % e)

    if len(raw) < (stride + 1) * height:
        raise PngError("IDAT too short")

    if depth >= 8:
        n = channels
        img = Image(width, height, "rgba" if ctype in (4, 6) else "rgb")
        stride = width * n
        prev = bytearray(stride)
        off = 0
        for y in range(height):
            f = raw[off]
            off += 1
            line = bytearray(raw[off : off + stride])
            off += stride
            if f == 0:
                pass
            elif f == 1:
                for i in range(bpp, stride):
                    line[i] = (line[i] + line[i - bpp]) & 0xFF
            elif f == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif f == 3:
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif f == 4:
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    b = prev[i]
                    c = prev[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
            else:
                raise PngError("bad filter %d" % f)
            base = y * width * n
            for i in range(width):
                o = i * n
                if n == 1:
                    v = line[o]
                    img.pixels[base + i * 3 : base + i * 3 + 3] = bytes((v, v, v))
                elif n == 2:
                    g = line[o]
                    a = line[o + 1]
                    img.pixels[base + i * 4 : base + i * 4 + 4] = bytes((g, g, g, a))
                else:
                    img.pixels[base + i * n : base + i * n + n] = bytes(line[o : o + n])
            prev = line
        return img

    # Palette / gray images with depth < 8: decode to RGB(A).
    img = Image(width, height, "rgb")
    if ctype == 3:
        if plte is None:
            raise PngError("palette image without PLTE")
        scale = 8 // depth
        npx = width * height
        bits = raw[0 : (stride + 1) * height]
        out = bytearray(npx)
        pos = 0
        idx = 0
        for y in range(height):
            f = bits[pos]
            pos += 1
            if f != 0:
                raise PngError("palette <8bit filter not 0")
            for k in range(stride):
                byte = bits[pos]
                pos += 1
                for s in range(scale):
                    if idx < npx:
                        out[idx] = (byte >> (8 - depth - s * depth)) & ((1 << depth) - 1)
                        idx += 1
        for i in range(npx):
            p = out[i] * 3
            img.pixels[i * 3 : i * 3 + 3] = bytes(plte[p : p + 3])
        return img
    raise PngError("unsupported combination depth=%d ctype=%d" % (depth, ctype))


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def save_png(img, bitdepth=8):
    """Encode an Image as PNG bytes (RGB or RGBA, Sub-filtered)."""
    w, h = img.w, img.h
    n = 4 if img.mode == "rgba" else 3
    ctype = 6 if n == 4 else 2
    stride = w * n
    raw = bytearray()
    for y in range(h):
        line = bytearray(img.pixels[y * stride : (y + 1) * stride])
        filtered = bytearray(stride + 1)
        filtered[0] = 1  # Sub filter
        for i in range(stride):
            a = line[i - n] if i >= n else 0
            filtered[i + 1] = (line[i] - a) & 0xFF
        raw += filtered

    def chunk(tag, body):
        c = struct.pack(">I", len(body)) + tag + body
        return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    out = bytearray(_SIG)
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bitdepth, ctype, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += chunk(b"IEND", b"")
    return bytes(out)
