# gif-test.py -- host gate for initramfs_extra/gif.py + mplayer GIF mode (M9.7).
# Generates real GIFs with Pillow, decodes them with gif.py and asserts
# pixel-exact frames, delays, transparency, disposal and interlacing; then
# drives mplayer.load_media + the player frame index with a kern stub.
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra", "apps"))

import gif as gifmod

passed = 0
failed = 0


def check(name, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print("gif-test PASS", name, extra)
    else:
        failed += 1
        print("gif-test FAIL", name, extra)


def pil_bytes(img):
    img = img.convert("RGB")
    return img.tobytes()


# --- 1. static 2x2 two-color GIF -------------------------------------
from PIL import Image
im = Image.new("P", (2, 2))
im.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255] + [0, 0, 0] * 253)
im.putdata([0, 1, 1, 2])
buf = io.BytesIO()
im.save(buf, "GIF")
data = buf.getvalue()
g = gifmod.decode(data)
check("static dims", (g.width, g.height) == (2, 2), str((g.width, g.height)))
check("static 1 frame", len(g.frames) == 1)
img0 = g.frames[0][0]
check("static pixels", bytes(img0.pixels) == pil_bytes(im),)

# --- 2. animated GIF with delays (Pillow save_all) -------------------
frames = [Image.new("P", (4, 4)), Image.new("P", (4, 4)), Image.new("P", (4, 4))]
pal = [0, 0, 0, 255, 255, 255] + [0, 0, 0] * 254
for i, f in enumerate(frames):
    f.putpalette(pal)
    f.putdata([i if (x + y) % 2 == 0 else 0 for y in range(4) for x in range(4)])
buf = io.BytesIO()
frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=[80, 160, 40], loop=0)
g = gifmod.decode(buf.getvalue())
check("anim 3 frames", len(g.frames) == 3, str(len(g.frames)))
check("anim delays", [d for (_i, d) in g.frames] == [80, 160, 40],
      str([d for (_i, d) in g.frames]))
for i in range(3):
    check("anim frame %d pixels" % i, bytes(g.frames[i][0].pixels) == pil_bytes(frames[i]))

# --- 3. transparency + disposal ---------------------------------------
im1 = Image.new("P", (3, 3))
im1.putpalette(pal)
im1.putdata([1, 1, 1, 1, 0, 1, 1, 1, 1])
im2 = Image.new("P", (3, 3))
im2.putpalette(pal)
im2.putdata([0, 0, 0, 0, 1, 0, 0, 0, 0])
buf = io.BytesIO()
im1.save(buf, "GIF", save_all=True, append_images=[im2], duration=[100, 100],
         transparency=0, disposal=2, loop=0)
g = gifmod.decode(buf.getvalue())
check("transparent 2 frames", len(g.frames) == 2)
# Pillow's composite view: frame0 white with black hole, frame1 black with white
check("transparency frame0", bytes(g.frames[0][0].pixels) == pil_bytes(im1))
check("transparency frame1", bytes(g.frames[1][0].pixels) == pil_bytes(im2))

# --- 4. interlaced GIF ------------------------------------------------
im = Image.new("P", (8, 8))
im.putpalette(pal)
im.putdata([(x + y) % 2 for y in range(8) for x in range(8)])
buf = io.BytesIO()
im.save(buf, "GIF", interlace=True)
g = gifmod.decode(buf.getvalue())
check("interlaced pixels", bytes(g.frames[0][0].pixels) == pil_bytes(im))

# --- 5. large palette GIF (>= 16 colors, 4-bit min code) --------------
im = Image.new("P", (16, 16))
pal2 = [(i, (i * 7) % 256, (i * 13) % 256) for i in range(64)]
flat = [c for rgb in pal2 for c in rgb] + [0, 0, 0] * (256 - len(pal2))
im.putpalette(flat)
im.putdata([(x + y * 3) % 64 for y in range(16) for x in range(16)])
buf = io.BytesIO()
im.save(buf, "GIF")
g = gifmod.decode(buf.getvalue())
check("64-color pixels", bytes(g.frames[0][0].pixels) == pil_bytes(im))

# --- 6. mplayer integration -------------------------------------------
class _Kern:
    def __init__(self):
        self.w, self.h, self.bpp, self.stride = 320, 240, 3, 320
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
    def tick(self):
        return 0
    def after(self, ms, cb):
        self.timers.append((ms, cb))
    def sleep(self, ms):
        pass
    def write(self, s):
        self.log.append(s)
    def speaker(self, hz):
        pass
    def speaker_off(self):
        pass
    def audio_ready(self):
        return False
    def audio_play(self, pcm, rate, ch, vol=128):
        return len(pcm)
    def audio_stop(self):
        pass
    def audio_busy(self):
        return 0
    def key_events(self):
        return []
    def on_key(self, cb):
        self.key_cb = cb
    def mouse_events(self):
        return []
    def on_mouse(self, cb):
        self.mouse_cb = cb
    def eval(self, src):
        return None

kern = _Kern()
kern.speaker_log = []
sys.modules["kern"] = kern

import mplayer
# regenerate the 3-frame animated GIF for the player path
frames = [Image.new("P", (4, 4)), Image.new("P", (4, 4)), Image.new("P", (4, 4))]
for i, f in enumerate(frames):
    f.putpalette(pal)
    f.putdata([i if (x + y) % 2 == 0 else 0 for y in range(4) for x in range(4)])
buf2 = io.BytesIO()
frames[0].save(buf2, "GIF", save_all=True, append_images=frames[1:],
               duration=[80, 160, 40], loop=0)
import tempfile
with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tf:
    gif_path = tf.name
    tf.write(buf2.getvalue())
kind, info, bars = mplayer.load_media(gif_path)
check("mplayer sniffs gif", kind == "gif", str(kind))
check("mplayer gif info", info["frames"] == 3 and info["codec_name"] == "GIF animation",
      str(info.get("frames")))
check("mplayer gif duration", abs(info["duration_s"] - 0.28) < 0.01,
      str(info.get("duration_s")))
check("mplayer gif bars", len(bars) == 3 and hasattr(bars[0][0], "blit"))

print("gif-test: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
