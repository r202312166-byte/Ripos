# png-test.py -- host gate for initramfs_extra/png.py (M9.2).
# Run with host python:  py -3 target/png-test.py
# 1. An INDEPENDENT 2x2 PNG (built by hand with host zlib/struct) must
#    decode correctly (validates the decoder against a second encoder).
# 2. save_png -> load_png roundtrip must be pixel-exact.
# 3. RGBA and palette paths.

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import png  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("png-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("png-test FAIL %s%s" % (name, extra))


def make_png(w, h, raw):
    """Independent encoder: filter-0 scanlines + zlib, CRC by hand."""
    sig = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    out = bytearray(sig)

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(
            ">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw)))
    out += chunk(b"IEND", b"")
    return bytes(out)


# --- decoder: 2x2 RGB with filter 0 ---
pixels = [
    (255, 0, 0), (0, 255, 0),
    (0, 0, 255), (255, 255, 0),
]
raw = bytearray()
for y in range(2):
    raw.append(0)
    for x in range(2):
        raw += bytes(pixels[y * 2 + x])
data = make_png(2, 2, raw)
img = png.load_png(data)
check("decoder 2x2 RGB size", (img.w, img.h) == (2, 2), " got %r" % ((img.w, img.h),))
ok = all(img.get(x, y) == pixels[y * 2 + x] for y in range(2) for x in range(2))
check("decoder 2x2 RGB pixels", ok)

# --- decoder: filter types 1..4 on a gradient ---
w, h = 8, 8
src = [(x * 30 % 256, y * 30 % 256, (x + y) * 17 % 256) for y in range(h) for x in range(w)]
raw = bytearray()
for y in range(h):
    f = (y % 4) + 1  # sub, up, average, paeth
    raw.append(f)
    line = bytearray()
    for x in range(w):
        p = src[y * w + x]
        line += bytes(p)
    # apply the inverse of filter f to make the stored bytes
    prev = bytearray()
    for i, (r, g, b) in enumerate([src[y * w + x] for x in range(w)]):
        pass
    # simpler: build filtered bytes properly
    filt = bytearray()
    for x in range(w):
        r, g, b = src[y * w + x]
        rp, gp, bp = src[y * w + x - 1] if x > 0 else (0, 0, 0)
        ru, gu, bu = src[(y - 1) * w + x] if y > 0 else (0, 0, 0)
        rpu, gpu, bpu = src[(y - 1) * w + x - 1] if (y > 0 and x > 0) else (0, 0, 0)
        if f == 1:
            filt += bytes(((r - rp) & 0xFF, (g - gp) & 0xFF, (b - bp) & 0xFF))
        elif f == 2:
            filt += bytes(((r - ru) & 0xFF, (g - gu) & 0xFF, (b - bu) & 0xFF))
        elif f == 3:
            filt += bytes(((r - ((rp + ru) >> 1)) & 0xFF,
                           (g - ((gp + gu) >> 1)) & 0xFF,
                           (b - ((bp + bu) >> 1)) & 0xFF))
        else:
            def paeth(a, b, c):
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            filt += bytes(((r - paeth(rp, ru, rpu)) & 0xFF,
                           (g - paeth(gp, gu, gpu)) & 0xFF,
                           (b - paeth(bp, bu, bpu)) & 0xFF))
    raw += filt
data = make_png(w, h, raw)
img = png.load_png(data)
ok = all(img.get(x, y) == src[y * w + x] for y in range(h) for x in range(w))
check("decoder filters 1-4", ok)

# --- roundtrip via our own encoder ---
img2 = png.Image(16, 12, "rgb")
for y in range(12):
    for x in range(16):
        img2.set(x, y, ((x * 16) % 256, (y * 21) % 256, (x * y) % 256))
enc = png.save_png(img2)
img3 = png.load_png(enc)
check("roundtrip size", (img3.w, img3.h) == (16, 12))
ok = all(img3.get(x, y) == img2.get(x, y) for y in range(12) for x in range(16))
check("roundtrip pixels", ok)

# --- RGBA roundtrip ---
img4 = png.Image(4, 4, "rgba")
for y in range(4):
    for x in range(4):
        img4.set(x, y, (x * 60, y * 60, 128))
# set alpha explicitly
for i in range(0, len(img4.pixels), 4):
    img4.pixels[i + 3] = (i // 4) * 17 % 256
enc4 = png.save_png(img4)
img5 = png.load_png(enc4)
ok = all(img5.get(x, y) == img4.get(x, y) for y in range(4) for x in range(4))
check("rgba roundtrip", ok)

# --- bad data ---
try:
    png.load_png(b"not a png")
    check("bad signature rejected", False)
except png.PngError:
    check("bad signature rejected", True)

print("png-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
