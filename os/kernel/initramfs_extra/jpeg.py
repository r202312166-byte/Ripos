# jpeg.py -- minimal pure-Python baseline-JPEG decoder for Ripos (M9.2).
#
# Decodes baseline sequential JPEG (SOF0, 8-bit, 1-4 components, arbitrary
# sampling factors, restart intervals).  Progressive (SOF2) and 12-bit
# (SOF1+precision>8) are rejected with a clear error.  Output is a png.Image
# ('rgb'), ready for imgview.py / framebuffer drawing.
#
# Implementation notes:
#  - Entropy data is decoded MSB-first with 0xFF 0x00 unstuffing and RSTn
#    restart detection (byte-aligned at MCU boundaries, DC predictors reset).
#  - Huffman tables are built canonically from DHT count/symbol arrays.
#  - The IDCT is the classic separable float IDCT (JPEG reference math),
#    which is exact enough for a viewer; the zig-zag order of the entropy
#    coefficients is converted to natural order before the transform.
#  - Subsampled chroma is upsampled by pixel replication (nearest).
#  - Performance: pure Python, so big photos are slow -- the M9.2 plan has a
#    zig-built libjpeg C module as the fast path if that becomes a problem.

import math
import struct

from png import Image


class JpegError(Exception):
    pass


# Zig-zag order: position of the coefficient that is k-th in natural order.
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)
# Natural index of the coefficient stored at zig-zag position z.
UNZIGZAG = [0] * 64
for _i in range(64):
    UNZIGZAG[ZIGZAG[_i]] = _i

_IDCT_MAT = None


def _idct_mat():
    """8x8 cosine matrix for the JPEG-spec IDCT.

    x(n,m) = (1/4) sum_u sum_v C(u) C(v) F(u,v) cos((2n+1)u pi/16) cos((2m+1)v pi/16)
    with C(0)=1/sqrt(2).  The 1/4 factor is folded in as 0.5 per pass (the
    transform is separable: two 1-D passes of the same matrix)."""
    global _IDCT_MAT
    if _IDCT_MAT is None:
        m = [[0.0] * 8 for _ in range(8)]
        for u in range(8):
            c = 0.7071067811865476 if u == 0 else 1.0
            for n in range(8):
                m[u][n] = 0.5 * c * math.cos((2 * n + 1) * u * math.pi / 16.0)
        _IDCT_MAT = m
    return _IDCT_MAT


def _idct_8x8(block):
    """Separable 2-D float IDCT of a 64-float block in natural order."""
    mat = _idct_mat()
    tmp = [0.0] * 64
    for i in range(8):
        b0 = i * 8
        for j in range(8):
            s = 0.0
            for u in range(8):
                s += block[b0 + u] * mat[u][j]
            tmp[b0 + j] = s
    out = [0.0] * 64
    for j in range(8):
        for i in range(8):
            s = 0.0
            for v in range(8):
                s += tmp[v * 8 + j] * mat[v][i]
            out[i * 8 + j] = s
    return out


def _build_huff(counts, symbols):
    """Canonical Huffman table: {(code_length, code): symbol}."""
    table = {}
    code = 0
    k = 0
    for length in range(1, 17):
        n = counts[length - 1]
        for _ in range(n):
            table[(length, code)] = symbols[k]
            code += 1
            k += 1
        code <<= 1
    return table


class _Bits:
    """MSB-first entropy bit reader with 0xFF-stuffing and restart support."""

    __slots__ = ("data", "pos", "end", "bitbuf", "bitcnt", "restart", "marker")

    def __init__(self, data, start, end):
        self.data = data
        self.pos = start
        self.end = end
        self.bitbuf = 0
        self.bitcnt = 0
        self.restart = None   # RSTn seen (0xD0..0xD7) or None
        self.marker = None    # non-RST marker seen (0xD9 etc.) or None

    def _read_byte(self):
        if self.pos >= self.end:
            self.marker = 0xD9
            return None
        b = self.data[self.pos]
        self.pos += 1
        if b != 0xFF:
            return b
        if self.pos >= self.end:
            self.marker = 0xD9
            return None
        b2 = self.data[self.pos]
        self.pos += 1
        if b2 == 0x00:
            return 0xFF
        if 0xD0 <= b2 <= 0xD7:
            self.restart = b2
            return None
        self.marker = b2
        return None

    def get_bit(self):
        if self.bitcnt == 0:
            b = self._read_byte()
            if b is None:
                b = 0
            self.bitbuf = b
            self.bitcnt = 8
        self.bitcnt -= 1
        return (self.bitbuf >> self.bitcnt) & 1

    def get_bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.get_bit()
        return v

    def byte_align(self):
        self.bitcnt = 0
        self.bitbuf = 0


def _huff_decode(bits, table):
    code = 0
    for length in range(1, 17):
        code = (code << 1) | bits.get_bit()
        sym = table.get((length, code))
        if sym is not None:
            return sym
    raise JpegError("invalid Huffman code")


def _receive(bits, s):
    v = bits.get_bits(s)
    if v < (1 << (s - 1)):
        v -= (1 << s) - 1
    return v


def _decode_block(bits, dc_pred, ci, dc_tbl, ac_tbl):
    """One 8x8 block of quantized coefficients in ZIG-ZAG order."""
    block = [0] * 64
    t = _huff_decode(bits, dc_tbl)
    if t:
        block[0] = dc_pred[ci] + _receive(bits, t)
        dc_pred[ci] = block[0]
    else:
        block[0] = dc_pred[ci]
    k = 1
    while k < 64:
        rs = _huff_decode(bits, ac_tbl)
        r = rs >> 4
        s = rs & 15
        if s == 0:
            if r == 15:
                k += 16
            else:
                break  # EOB
        else:
            k += r
            if k >= 64:
                break
            block[k] = _receive(bits, s)
            k += 1
    return block


def _upsample_convert(planes, comps, width, height, hmax, vmax):
    img = Image(width, height, "rgb")
    px = img.pixels
    if len(comps) == 1:
        p = planes[0]
        gw = comps[0]["gw"]
        for y in range(height):
            base = y * width * 3
            row = y * gw
            for x in range(width):
                v = p[row + x]
                o = base + x * 3
                px[o] = px[o + 1] = px[o + 2] = v
        return img
    # 3 components: YCbCr.
    yp, ygw = planes[0], comps[0]["gw"]
    cbp, cbgw = planes[1], comps[1]["gw"]
    crp, crgw = planes[2], comps[2]["gw"]
    cbh, cbv = comps[1]["h"], comps[1]["v"]
    crh, crv = comps[2]["h"], comps[2]["v"]
    for y in range(height):
        base = y * width * 3
        yrow = y * ygw
        cbrow = (y * cbv // vmax) * cbgw
        crrow = (y * crv // vmax) * crgw
        for x in range(width):
            yy = yp[yrow + x]
            cbi = cbp[cbrow + x * cbh // hmax]
            cri = crp[crrow + x * crh // hmax]
            r = yy + 1.402 * (cri - 128)
            g = yy - 0.344136 * (cbi - 128) - 0.714136 * (cri - 128)
            b = yy + 1.772 * (cbi - 128)
            o = base + x * 3
            px[o] = 255 if r > 255 else (0 if r < 0 else int(r + 0.5))
            px[o + 1] = 255 if g > 255 else (0 if g < 0 else int(g + 0.5))
            px[o + 2] = 255 if b > 255 else (0 if b < 0 else int(b + 0.5))
    return img


def _decode_scan(frame, scan, qtables, huff, data, start, end, restart_interval):
    width, height = frame["w"], frame["h"]
    comps = frame["comps"]
    for sid, dt, at in scan:
        c = comps[sid - 1]
        c["dc"] = dt
        c["ac"] = at
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    mcu_w = 8 * hmax
    mcu_h = 8 * vmax
    mcus_x = (width + mcu_w - 1) // mcu_w
    mcus_y = (height + mcu_h - 1) // mcu_h

    planes = []
    for c in comps:
        gw = mcus_x * c["h"] * 8
        gh = mcus_y * c["v"] * 8
        c["gw"] = gw
        planes.append(bytearray(gw * gh))

    bits = _Bits(data, start, end)
    dc_pred = [0] * len(comps)
    restart_left = restart_interval

    for my in range(mcus_y):
        for mx in range(mcus_x):
            if bits.marker is not None:
                break
            bits.restart = None
            for ci, c in enumerate(comps):
                dt = huff[(0, c["dc"])]
                at = huff[(1, c["ac"])]
                q = qtables[c["q"]]
                for by in range(c["v"]):
                    for bx in range(c["h"]):
                        block = _decode_block(bits, dc_pred, ci, dt, at)
                        nat = [0.0] * 64
                        # block[k] is the coefficient at zig-zag position k;
                        # ZIGZAG maps position -> natural index.
                        for k in range(64):
                            nat[ZIGZAG[k]] = block[k] * q[k]  # q is zig-zag ordered
                        vals = _idct_8x8(nat)
                        plane = planes[ci]
                        gw = c["gw"]
                        ox = (mx * c["h"] + bx) * 8
                        oy = (my * c["v"] + by) * 8
                        for j in range(8):
                            base = (oy + j) * gw + ox
                            o8 = j * 8
                            for i in range(8):
                                v = vals[o8 + i] + 128.0
                                plane[base + i] = 255 if v > 255 else (0 if v < 0 else int(v + 0.5))
            if restart_interval:
                restart_left -= 1
                if restart_left == 0:
                    bits.byte_align()
                    dc_pred = [0] * len(comps)
                    restart_left = restart_interval
    return _upsample_convert(planes, comps, width, height, hmax, vmax)


def decode_jpeg(data):
    """Decode a baseline-JPEG byte string into a png.Image (mode 'rgb').

    Uses the kernel's C stb_image decoder (_img) when present -- MJPEG video
    then decodes at C speed -- and falls back to the pure-Python decoder
    (host test runner, or formats the C decoder rejects)."""
    if not data.startswith(b"\xff\xd8"):
        raise JpegError("not a JPEG")
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
    pos = 2
    n = len(data)
    qtables = {}
    huff = {}
    frame = None
    restart_interval = 0
    img = None

    while pos + 2 <= n:
        while data[pos] == 0xFF:
            pos += 1
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > n:
            raise JpegError("truncated marker")
        length = struct.unpack(">H", data[pos : pos + 2])[0]
        body = data[pos + 2 : pos + length]
        pos += length
        if marker == 0xC0:  # SOF0 baseline
            prec, height, width, ncomp = body[0], struct.unpack(">H", body[1:3])[0], struct.unpack(">H", body[3:5])[0], body[5]
            if prec != 8:
                raise JpegError("only 8-bit baseline JPEG supported (precision=%d)" % prec)
            comps = []
            for i in range(ncomp):
                comps.append({"id": body[6 + i * 3], "h": body[7 + i * 3] >> 4, "v": body[7 + i * 3] & 15, "q": body[8 + i * 3]})
            frame = {"w": width, "h": height, "comps": comps}
        elif marker in (0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            # non-baseline SOF markers (extended/progressive/lossless/differential)
            raise JpegError("unsupported JPEG marker 0x%02X (baseline only)" % marker)
        elif marker == 0xDB:  # DQT
            i = 0
            blen = len(body)
            while i < blen:
                pq_tq = body[i]
                i += 1
                pq = pq_tq >> 4
                tq = pq_tq & 15
                if pq == 0:
                    vals = list(body[i : i + 64])
                    i += 64
                else:
                    vals = [struct.unpack(">H", body[i + 2 * j : i + 2 * j + 2])[0] for j in range(64)]
                    i += 128
                qtables[tq] = vals  # keep zig-zag order: block[k] is the k-th zig-zag coefficient
        elif marker == 0xC4:  # DHT
            i = 0
            blen = len(body)
            while i < blen:
                tc_th = body[i]
                i += 1
                tc = tc_th >> 4
                th = tc_th & 15
                counts = list(body[i : i + 16])
                i += 16
                symbols = list(body[i : i + sum(counts)])
                i += sum(counts)
                huff[(tc, th)] = _build_huff(counts, symbols)
        elif marker == 0xDD:  # DRI
            restart_interval = struct.unpack(">H", body[0:2])[0]
        elif marker == 0xDA:  # SOS
            if frame is None:
                raise JpegError("SOS before SOF")
            ns = body[0]
            scan = []
            for i in range(ns):
                sid = body[1 + i * 2]
                tbl = body[2 + i * 2]
                scan.append((sid, tbl >> 4, tbl & 15))
            # entropy-coded data runs until the next marker (FF xx, xx not 00/RST)
            end = pos
            while end < n:
                b = data[end]
                if b == 0xFF:
                    b2 = data[end + 1] if end + 1 < n else 0xFF
                    if b2 == 0x00 or 0xD0 <= b2 <= 0xD7:
                        end += 2
                        continue
                    break
                end += 1
            img = _decode_scan(frame, scan, qtables, huff, data, pos, end, restart_interval)
            pos = end
        # APPn / COM / others: body already skipped
    if img is None:
        raise JpegError("no image data found")
    return img
