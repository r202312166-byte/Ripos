# mplayer-test.py -- host gate for initramfs_extra/mp3.py + apps/mplayer.py
# (M9.5).  Stubs kern (fake framebuffer), runs the REAL parser and player
# code, and asserts:
#   - MP3 frame scanning: duration/bitrate/samplerate/channels/frame count
#   - ID3v2 text frames parse; energy_map returns sane 0..1 bars
#   - load_media sniffs MP3 vs MP4 magic
#   - the Player draws the header panel, waveform, playhead, volume meter
#     and status bar; the tick advances the playhead while playing
#   - space/arrows/Home/vol keys, wheel/click/drag seek
#   - Esc quits: kernel keyboard restored + on_quit fires
#   - MP4 mode: frame index/cumulative durations, frame-step, chart drawn
#   - run() end-to-end with an MP3 path
# Run with host python:  py -3 target/mplayer-test.py
import io
import os
import pathlib
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra", "apps"))


class _Kern:
    def __init__(self):
        self.w = 320
        self.h = 240
        self.bpp = 3
        self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
        self.timers = []
        self.key_cb = None
        self.mouse_cb = None

    def fb_info(self):
        return (self.w, self.h, self.stride, self.bpp, 0)

    def fb_mem(self):
        return self.mem

    def fb_set_mode(self, w, h):
        return None

    def write(self, s):
        self.log.append(s)

    def after(self, ms, cb):
        self.timers.append((ms, cb))

    def tick(self):
        return 0

    def on_key(self, cb):
        self.key_cb = cb

    def on_mouse(self, cb):
        self.mouse_cb = cb

    def speaker(self, hz):
        self.speaker_log.append(hz)

    def speaker_off(self):
        self.speaker_log.append(0)


kern = _Kern()
kern.speaker_log = []
sys.modules['kern'] = kern

import mp3                       # noqa: E402
import mplayer                   # noqa: E402
import appbar                    # noqa: E402
import settings                  # noqa: E402
from framebuffer import Framebuffer   # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("mplayer-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("mplayer-test FAIL %s%s" % (name, extra))


class _KB:
    def __init__(self):
        self.dispatches = 0

    def _dispatch(self, ev):
        self.dispatches += 1


# ---- 1. synthetic MPEG1 Layer III stream ------------------------------
def mp3_frame(i):
    # 0xFF sync, MPEG1 Layer III, no CRC, 128 kbps, 44100 Hz, stereo
    header = bytes((0xFF, 0xFB, 0x90, 0x00))
    blen = 144 * 128000 // 44100   # 417-byte frames
    payload = bytes((j * 7 + i * 3) % 256 for j in range(blen - 4))
    return header + payload


mp3bytes = b"".join(mp3_frame(i) for i in range(10))
info = mp3.get_info(mp3bytes)
check("mp3 frame stream parses", info["frames"] == 10, " got %r" % info)
check("mp3 duration", abs(info["duration_s"] - 10 * 1152 / 44100.0) < 0.0001,
      " got %r" % (info["duration_s"],))
check("mp3 bitrate", info["bitrate"] == 128000, " got %r" % info["bitrate"])
check("mp3 samplerate", info["samplerate"] == 44100, " got %r" % info["samplerate"])
check("mp3 channels", info["channels"] == 2, " got %r" % info["channels"])

bars = mp3.energy_map(mp3bytes, 32)
check("energy_map size", len(bars) == 32, " got %d" % len(bars))
check("energy_map in range", all(0.0 <= b <= 1.0 for b in bars)
      and any(b > 0.01 for b in bars), " got %r" % bars[:4])

# ID3v2 padded tag in front still scans the audio
id3 = b"ID3\x03\x00\x00" + bytes(((25 >> 21) & 0x7F, (25 >> 14) & 0x7F,
                                   (25 >> 7) & 0x7F, 25 & 0x7F))
id3 += b"TIT2\x00\x00\x00\x05\x00\x00" + b"\x01A\x00B\x00"   # UTF-16 title "AB"
id3 += b"\x00" * 10   # padding
withid3 = id3 + mp3bytes
info2 = mp3.get_info(withid3)
check("id3v2 audio_start skips tag", info2["audio_start"] == len(id3),
      " got %d want %d" % (info2["audio_start"], len(id3)))
check("id3v2 frames parse", info2["frames"] == 10, " got %r" % info2["frames"])
check("id3v2 text decode", info2["title"] == "AB", " got %r" % info2["title"])

# ---- 2. load_media sniffing + Mp4 build --------------------------------
mp3tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
mp3tmp.write(withid3)
mp3tmp.close()

kind, info3, bars3 = mplayer.load_media(mp3tmp.name)
check("load_media mp3", kind == "mp3" and info3["frames"] == 10
      and len(bars3) == 256, " got %r" % ((kind, info3, len(bars3)),))


def box(btype, payload):
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def u32(v):
    return struct.pack(">I", v)


W, H, TS, N = 64, 48, 1000, 5
FRAMES = [bytes([i * 40, 100, 200]) * (W * H) for i in range(N)]
SIZES = [len(f) for f in FRAMES]
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
stsz = box(b"stsz", b"\x00" * 4 + u32(0) + u32(N) + b"".join(u32(s) for s in SIZES))
stsc = box(b"stsc", b"\x00" * 4 + u32(1) + u32(1) + u32(N) + u32(1))
stco = box(b"stco", b"\x00" * 4 + u32(1) + u32(0))
vmhd = box(b"vmhd", b"\x00" * 4 + b"\x00" * 8)
stbl = box(b"stbl", stsd + stts + stsz + stsc + stco)
minf = box(b"minf", vmhd + stbl)
mdia = box(b"mdia", mdhd + hdlr + minf)
trak = box(b"trak", tkhd + mdia)
moov = box(b"moov", mvhd + trak)
mdat_payload_off = len(ftyp) + len(moov) + 8
stco = box(b"stco", b"\x00" * 4 + u32(1) + u32(mdat_payload_off))
stbl = box(b"stbl", stsd + stts + stsz + stsc + stco)
minf = box(b"minf", vmhd + stbl)
mdia = box(b"mdia", mdhd + hdlr + minf)
trak = box(b"trak", tkhd + mdia)
moov = box(b"moov", mvhd + trak)
mp4bytes = ftyp + moov + box(b"mdat", b"".join(FRAMES))
mp4tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
mp4tmp.write(mp4bytes)
mp4tmp.close()

kind, info4, frames4 = mplayer.load_media(mp4tmp.name)
check("load_media mp4", kind == "mp4" and info4["frames"] == N
      and len(frames4) == N, " got %r" % ((kind, info4, len(frames4)),))
check("mp4 info dims", int(info4["width"]) == W and int(info4["height"]) == H,
      " got %r" % ((info4.get("width"), info4.get("height")),))

# ---- 3. player screen layout -------------------------------------------
fb = Framebuffer()
caller = _KB()
quits = []


def on_quit():
    quits.append(1)


root = mplayer.Player("mp3", info3, bars3, "/home/test.mp3", caller, on_quit)
kb2 = __import__("keyboard").Keyboard()
root.start(fb, kb2, None)

cav_x, cav_y, cav_w, cav_h = 92 + 18, 16 + 18, 320 - 92 - 0 - 36, 240 - 16 - 15 - 36
check("cavity matches", (cav_x, cav_y, cav_w, cav_h) == (110, 34, 192, 173)
      and appbar.right_sidebar_w() == 0, "")
check("panel drawn", fb.pixel(200, 50) == mplayer.PANEL,
      " got %r" % (fb.pixel(200, 50),))
check("playhead at start", fb.pixel(130, 97) == mplayer.PLAYHEAD,
      " got %r" % (fb.pixel(130, 97),))
# seek to the middle: front bars go green, far-right bars stay grey
root.on_mouse((200, 120, 1, 1, 0, 1, 0))
check("waveform bars drawn", fb.pixel(132, 156) in (mplayer.GREEN_LO,
                                                    mplayer.GREEN_HI),
      " got %r" % (fb.pixel(132, 156),))
check("waveform 'tail' grey", fb.pixel(275, 156) == mplayer.GREY_BAR,
      " got %r" % (fb.pixel(275, 156),))
check("volume meter green", fb.pixel(214, 68) == mplayer.GREEN_HI,
      " got %r" % (fb.pixel(214, 68),))
check("status bar", fb.pixel(94, 236) == (10, 13, 18),
      " got %r" % (fb.pixel(94, 236),))

# ---- 4. keys -----------------------------------------------------------
root.pos_s = 0.0
root.on_key(("space", 32, True))
check("space toggles pause", root.playing is False, " got %s" % root.playing)
root.on_key(("up", 0, True))
check("volume up", abs(root.vol - 0.8) < 0.001, " got %r" % root.vol)
root.on_key(("down", 0, True))
check("volume down", abs(root.vol - 0.7) < 0.001, " got %r" % root.vol)
root.pos_s = 0.2
root.on_key(("left", 0, True))
check("left seek wraps/back", root.pos_s == 0.0, " got %r" % root.pos_s)
root.on_key(("right", 0, True))
check("right seek wraps/fwd", root.pos_s == 0.0, " got %r" % root.pos_s)
root.on_key(("home", 0, True))
check("home seeks to start", root.pos_s == 0.0, " got %r" % root.pos_s)
root.on_key(("space", 32, True))
check("space toggles play", root.playing is True, " got %s" % root.playing)

# ---- 5. mouse seek -----------------------------------------------------
root.on_mouse((160, 120, 0, 0, 0, 0, 120))     # wheel up: +5s wraps to 0
check("wheel seek wraps", root.pos_s == 0.0, " got %r" % root.pos_s)
root.on_mouse((200, 120, 1, 1, 0, 1, 0))       # click timeline at x=200
frac = (200 - 130) / 152.0
want = round(frac * root._duration(), 4)
check("click seeks", abs(root.pos_s - want) < 0.01, " got %r want %r"
      % (root.pos_s, want))
root.on_mouse((260, 120, 0, 0, 0, 0, 0))       # drag right
# drag seeks by frame-relative x (_frac_of_x: (x-cav_x)/(cav_w-40))
frac2 = (260 - 110) / 152.0
want2 = frac2 * root._duration()
check("drag seeks", abs(root.pos_s - want2) < 0.01, " got %r want %r"
      % (root.pos_s, want2))
root.on_mouse((260, 120, 1, 0, 0, 0, 0))
check("release ends drag", root._dragging is False, "")

# ---- 6. tick advances the playhead -------------------------------------
root.pos_s = 0.0
n = len(kern.timers)
root._advance()
check("tick schedules timer", len(kern.timers) == n + 1, "")
check("tick advances pos", root.pos_s == 0.25, " got %r" % root.pos_s)
root.playing = False
p = root.pos_s
root._advance()
check("paused tick holds pos", root.pos_s == p, " got %r" % root.pos_s)
root.destroy()
quits.clear()

# ---- 7. quit restores keyboard -----------------------------------------
root2 = mplayer.Player("mp3", info3, bars3, "/home/test.mp3", caller, on_quit)
kb3 = __import__("keyboard").Keyboard()
root2.start(fb, kb3, None)
root2.on_key(("esc", 27, True))
check("quit destroys", root2.destroyed, "")
check("quit calls on_quit", len(quits) == 1, "")
check("quit restores keyboard", kern.key_cb == caller._dispatch, "")

# ---- 8. MP4 frame stepping ---------------------------------------------
root4 = mplayer.Player("mp4", info4, frames4, "/home/test.mp4", caller, on_quit)
check("mp4 cumulative ticks", root4._cum == [40, 80, 120, 160, 200],
      " got %r" % root4._cum)
check("mp4 duration", abs(root4._duration() - 0.2) < 0.001,
      " got %r" % root4._duration())
root4.on_key(("right", 0, True))
check("mp4 step next", root4._seq == 1 and abs(root4.pos_s - 0.08) < 0.001,
      " got %r %r" % (root4._seq, root4.pos_s))
root4.on_key(("right", 0, True))
root4.on_key(("left", 0, True))
check("mp4 step back", root4._seq == 1, " got %r" % root4._seq)
root4.start(fb, kb3, None)
check("mp4 chart bars drawn", fb.pixel(132, 156) == mplayer.GREEN_HI,
      " got %r" % (fb.pixel(132, 156),))
root4.destroy()

# ---- 9. run() end-to-end -----------------------------------------------
kern.mem = memoryview(bytearray(kern.w * kern.h * kern.bpp))
kern.key_cb = None
quits.clear()
mplayer.run(fb, caller, None, on_quit=on_quit, path=mp3tmp.name)
check("run logs player", any("mplayer:" in s for s in kern.log), "")
check("run no empty screen", fb.pixel(200, 50) == mplayer.PANEL,
      " got %r" % (fb.pixel(200, 50),))


# ---- 10. M9.5: sound out (tone track + speaker) + video frame display ----

# tone track from a synthetic PCM buffer (host has no _mp3dec, so inject
# PCM directly into info and let the Player build the tone track)
pcm = bytearray()
import array as _arr
a = _arr.array('h')
# 1 second of a 440 Hz square-ish wave at 8000 Hz mono
for _i in range(8000):
    a.append(3000 if (_i // 9) % 2 == 0 else -3000)
pcm = a.tobytes()
info_s = dict(info3)
info_s['pcm'] = pcm
info_s['rate'] = 8000
info_s['channels'] = 1
root_s = mplayer.Player('mp3', info_s, [0.5] * 32, '/home/snd.mp3', caller,
                        on_quit)
kb_s = __import__('keyboard').Keyboard()
root_s.start(fb, kb_s, None)
# the tone track is built lazily on the first speaker sync (playing)
root_s.playing = True
root_s.vol = 0.7
root_s.pos_s = 0.05
root_s._speaker_sync()
check('tone track built', root_s._tones is not None and len(root_s._tones) > 0,
      ' got %r' % (None if root_s._tones is None else len(root_s._tones)))
check('tone track freq in range', all(f == 0 or 40 <= f <= 2500
      for f, _d in root_s._tones), ' got %r' % root_s._tones[:3])
# playing + audible volume -> speaker driven
kern.speaker_log = []
root_s.playing = True
root_s.vol = 0.7
root_s.pos_s = 0.05
root_s._speaker_sync()
check('speaker driven while playing', any(h > 0 for h in kern.speaker_log),
      ' got %r' % kern.speaker_log)
# muted -> speaker off
kern.speaker_log = []
root_s.vol = 0.0
root_s._speaker_sync()
check('speaker silent when muted', kern.speaker_log[-1:] == [0],
      ' got %r' % kern.speaker_log)
# paused -> speaker off
kern.speaker_log = []
root_s.vol = 0.7
root_s.playing = False
root_s._speaker_sync()
check('speaker silent when paused', kern.speaker_log[-1:] == [0],
      ' got %r' % kern.speaker_log)
root_s.destroy()

# MP4 raw frame display: the synthetic MP4's frames are solid colors;
# frame 0 = (0, 100, 200), frame 3 = (120, 100, 200)
root4.start(fb, kb3, None)
root4._seq = 0
root4.pos_s = 0.0
img0 = root4._current_frame_image()
check('mp4 frame decoded', img0 is not None and img0.w == W and img0.h == H,
      ' got %r' % (img0,))
root4.redraw()
cav_x, cav_y, cav_w, cav_h = root4._cavity(fb)
vx, vy = cav_x + 20, cav_y + 60
hits = 0
for yy in range(vy + 2, vy + 40, 4):
    for xx in range(vx + 2, vx + 60, 4):
        px = fb.pixel(xx, yy)
        if abs(px[0] - 0) < 12 and abs(px[1] - 100) < 12 \
           and abs(px[2] - 200) < 12:
            hits += 1
check('mp4 video area shows frame 0', hits > 5, ' got %d' % hits)
root4._seq = 3
root4.redraw()
hits3 = 0
for yy in range(vy + 2, vy + 40, 4):
    for xx in range(vx + 2, vx + 60, 4):
        px = fb.pixel(xx, yy)
        if abs(px[0] - 120) < 12 and abs(px[1] - 100) < 12 \
           and abs(px[2] - 200) < 12:
            hits3 += 1
check('mp4 video area shows frame 3', hits3 > 5, ' got %d' % hits3)
root4.destroy()

print("mplayer-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)