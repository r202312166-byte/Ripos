# jpeg-test.py -- host gate for initramfs_extra/jpeg.py (M9.2).
# Run with host python:  py -3 target/jpeg-test.py
# Generates baseline JPEGs with Pillow (4:4:4, 4:2:2, 4:2:0, gray, tiny and
# odd-sized images), decodes them with jpeg.py and compares against Pillow's
# own decode of the same bytes.
#
# Tolerance notes: my decoder uses the classic float IDCT and nearest-neighbour
# chroma upsampling; libjpeg uses an integer IDCT and fancy (interpolating)
# chroma upsampling.  Both are valid JPEG decoders, so subsampled tests use
# smooth-chroma content and a tolerance that absorbs the upsampling-method
# difference; 4:4:4/gray tests are tight.

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import jpeg  # noqa: E402

from PIL import Image as PILImage  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("jpeg-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("jpeg-test FAIL %s%s" % (name, extra))


def make_image(w, h, smooth=False):
    """Gradient + hard edges + a color patch (exercises AC coefficients).
    smooth=True keeps chroma low-frequency (realistic photos) so the
    nearest-vs-fancy upsampling difference stays small."""
    im = PILImage.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            r = x * 255 // max(1, w - 1)
            g = y * 255 // max(1, h - 1)
            if smooth:
                b = (x + y) * 2  # no wrap: max (w+h-2)*2 < 256 for these sizes
            else:
                b = (x * y) % 256
                if x < w // 4 or y < h // 4:
                    b = 255  # hard edge
            px[x, y] = (r, g, b)
    return im


def decode_and_compare(name, im, quality=85, subsampling=2, tolerance=4, extra=""):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, subsampling=subsampling, progressive=False)
    data = buf.getvalue()
    try:
        got = jpeg.decode_jpeg(data)
    except Exception as e:  # noqa: BLE001
        check(name, False, " decode raised %r" % (e,))
        return
    ref = PILImage.open(io.BytesIO(data)).convert("RGB")
    if (got.w, got.h) != ref.size:
        check(name, False, " size got %r want %r" % ((got.w, got.h), ref.size))
        return
    rp = ref.load()
    maxd = 0
    bad = 0
    for y in range(got.h):
        for x in range(got.w):
            r, g, b = got.get(x, y)
            rr, gg, bb = rp[x, y]
            d = max(abs(r - rr), abs(g - gg), abs(b - bb))
            if d > maxd:
                maxd = d
            if d > tolerance:
                bad += 1
    check(name, bad == 0, " maxdiff=%d bad=%d%s" % (maxd, bad, extra))


# --- 4:4:4 RGB (subsampling=0), full-frequency test image ---
decode_and_compare("4:4:4 q85 (hard edges)", make_image(64, 48), quality=85, subsampling=0, tolerance=4)
# --- 4:2:0 RGB (the common case), smooth chroma ---
decode_and_compare("4:2:0 q85 smooth", make_image(64, 48, smooth=True), quality=85, subsampling=2, tolerance=10)
# --- 4:2:2 RGB, smooth chroma ---
decode_and_compare("4:2:2 q85 smooth", make_image(64, 48, smooth=True), quality=85, subsampling=1, tolerance=10)
# --- quality 100, 4:4:4 (nearly lossless; float-vs-integer IDCT rounding) ---
decode_and_compare("q100 4:4:4", make_image(32, 32), quality=100, subsampling=0, tolerance=4)
# --- tiny 2x2 (blocks are 8x8; padding must be cropped) ---
decode_and_compare("tiny 2x2", make_image(2, 2), quality=90, subsampling=0, tolerance=8, extra=" (padded blocks)")
# --- odd size, partial MCUs both axes, 4:4:4 ---
decode_and_compare("odd 23x17", make_image(23, 17), quality=85, subsampling=0, tolerance=6, extra=" (partial MCUs)")
# --- odd size, 4:2:0, smooth ---
decode_and_compare("odd 23x17 420", make_image(23, 17, smooth=True), quality=85, subsampling=2, tolerance=12, extra=" (partial MCUs)")

# --- gray ---
im = make_image(40, 40)
gray = PILImage.new("L", (40, 40))
gpx = gray.load()
for y in range(40):
    for x in range(40):
        gpx[x, y] = (x * 6 + y * 3) % 256
buf = io.BytesIO()
gray.save(buf, "JPEG", quality=85)
data = buf.getvalue()
got = jpeg.decode_jpeg(data)
ref = PILImage.open(io.BytesIO(data)).convert("RGB")
rp = ref.load()
bad = 0
maxd = 0
for y in range(got.h):
    for x in range(got.w):
        r, g, b = got.get(x, y)
        rr, gg, bb = rp[x, y]
        d = max(abs(r - rr), abs(g - gg), abs(b - bb))
        maxd = max(maxd, d)
        if d > 4:
            bad += 1
check("gray", (got.w, got.h) == (40, 40) and bad == 0, " maxdiff=%d bad=%d" % (maxd, bad))

# --- progressive JPEG must be rejected cleanly ---
im = make_image(16, 16)
buf = io.BytesIO()
im.save(buf, "JPEG", quality=80, progressive=True)
try:
    jpeg.decode_jpeg(buf.getvalue())
    check("progressive rejected", False, " (accepted SOF2!)")
except jpeg.JpegError:
    check("progressive rejected", True)
except Exception as e:  # noqa: BLE001
    check("progressive rejected", False, " wrong exception %r" % (e,))

# --- invalid data ---
try:
    jpeg.decode_jpeg(b"not a jpeg")
    check("bad signature rejected", False)
except jpeg.JpegError:
    check("bad signature rejected", True)

print("jpeg-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
