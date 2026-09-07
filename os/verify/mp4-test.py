# mp4-test.py -- host gate for initramfs_extra/mp4.py (M9.5).
# Builds a tiny MP4 by hand (ftyp + moov with one raw-video track + mdat)
# and verifies the parser's metadata and frame index.
import io
import os
import pathlib
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import mp4  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("mp4-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("mp4-test FAIL %s%s" % (name, extra))


def box(btype, payload):
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def u32(v):
    return struct.pack(">I", v)


W, H, TS, N = 64, 48, 1000, 5
FRAMES = [bytes([i * 40, 100, 200]) * (W * H) for i in range(N)]  # raw RGB frames
SIZES = [len(f) for f in FRAMES]

# build the atom tree
ftyp = box(b"ftyp", b"isom" + u32(0) + b"isom" + b"avc1")
mvhd = box(b"mvhd", b"\x00" * 4 + u32(0) + u32(0) + u32(TS) + u32(TS * 2)
           + struct.pack(">IH", 0x00010000, 0x0100) + b"\x00" * 10 + b"\x00" * 36
           + b"\x00" * 24 + u32(1))
tkhd = box(b"tkhd", b"\x00" * 4 + u32(0) + u32(0) + u32(1) + u32(0) + u32(TS)
           + b"\x00" * 8 + b"\x00" * 2 + b"\x00" * 2 + b"\x00" * 2 + b"\x00" * 2
           + b"\x00" * 36 + struct.pack(">II", W << 16, H << 16))
mdhd = box(b"mdhd", b"\x00" * 4 + u32(0) + u32(0) + u32(TS) + u32(TS * 2)
           + b"\x00" * 2 + b"\x00" * 2)
hdlr = box(b"hdlr", b"\x00" * 4 + u32(0) + b"vide" + b"\x00" * 12)
stsd = box(b"stsd", b"\x00" * 4 + u32(1)
           + box(b"raw ", struct.pack(">IH", 6, 1) + u32(W) + u32(H)
                 + struct.pack(">IH", 24, 0x18) + b"\xff\xff\xff\xff"
                 + u32(0) + b"\x00" * 16))
stts = box(b"stts", b"\x00" * 4 + u32(1) + u32(N) + u32(40))
stsz = box(b"stsz", b"\x00" * 4 + u32(0) + u32(N)
           + b"".join(u32(s) for s in SIZES))
stsc = box(b"stsc", b"\x00" * 4 + u32(1) + u32(1) + u32(N) + u32(1))
# chunk offsets: mdat payload starts at a known position; compute after layout
# place mdat payload right after the moov box
# (we build moov first with placeholder offsets, then fix them)
stco = box(b"stco", b"\x00" * 4 + u32(1) + u32(0))  # patched below
vmhd = box(b"vmhd", b"\x00" * 4 + b"\x00" * 8)
stbl = box(b"stbl", stsd + stts + stsz + stsc + stco)
minf = box(b"minf", vmhd + stbl)
mdia = box(b"mdia", mdhd + hdlr + minf)
trak = box(b"trak", tkhd + mdia)
moov = box(b"moov", mvhd + trak)
mdat_payload_off = len(ftyp) + len(moov) + 8
stco_fixed = box(b"stco", b"\x00" * 4 + u32(1) + u32(mdat_payload_off))
stbl = box(b"stbl", stsd + stts + stsz + stsc + stco_fixed)
minf = box(b"minf", vmhd + stbl)
mdia = box(b"mdia", mdhd + hdlr + minf)
trak = box(b"trak", tkhd + mdia)
moov = box(b"moov", mvhd + trak)
mdat_payload_off = len(ftyp) + len(moov) + 8
stco_fixed = box(b"stco", b"\x00" * 4 + u32(1) + u32(mdat_payload_off))
stbl = box(b"stbl", stsd + stts + stsz + stsc + stco_fixed)
minf = box(b"minf", vmhd + stbl)
mdia = box(b"mdia", mdhd + hdlr + minf)
trak = box(b"trak", tkhd + mdia)
moov = box(b"moov", mvhd + trak)
mdat_payload_off = len(ftyp) + len(moov) + 8

mdat = box(b"mdat", b"".join(FRAMES))
mp4bytes = ftyp + moov + mdat

m = mp4.Mp4File(mp4bytes)
tr = m.video_track()
check("has video track", tr is not None, "")
check("codec raw", tr["codec"] == b"raw ", " got %r" % (tr["codec"],))
check("dimensions", (int(tr["width"]), int(tr["height"])) == (W, H),
      " got %r" % ((int(tr["width"]), int(tr["height"])),))
check("fps", abs(tr["fps"] - 25.0) < 0.01, " got %r" % (tr["fps"],))
check("duration", abs(m.duration_s - 0.2) < 0.001, " got %r" % (m.duration_s,))
fr = m.frames()
check("frame count", len(fr) == N, " got %d" % len(fr))
ok = all(m.read_frame(i) == FRAMES[i] for i in range(N))
check("frame payloads", ok, "")
# frame 2 offset must point into mdat at the right place
f2 = fr[2]
check("frame offset", f2["offset"] == mdat_payload_off + sum(SIZES[:2]),
      " got %d want %d" % (f2["offset"], mdat_payload_off + sum(SIZES[:2])))

# truncated/bad file
try:
    mp4.Mp4File(b"not an mp4 at all, no moov box here")
    check("no moov rejected", False)
except mp4.Mp4Error:
    check("no moov rejected", True)

print("mp4-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)