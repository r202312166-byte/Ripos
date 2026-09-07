# mplayer.py -- media player for Ripos (M9.5).
#
# A pure-Python music / video-track player for MP3 (mp3.py) and MP4 /
# ISO-BMFF (mp4.py).  This OS has no audio or video DMA and the embedded
# interpreter is freestanding, so "playback" is fully visual:
#   MP3  tag + format panel, an energy waveform from the frame payloads,
#        an animated playhead, seek (click/drag/wheel), vol meter
#   MP4  track metadata + a per-frame bitrate chart, animated playhead,
#        frame step through the sample index (read_frame for JPEG/raw
#        streams; H.264/avc1 keeps the index + metadata)
# Controls:
#   Esc (or q)    quit back to the caller (shell / file manager)
#   Space         play / pause
#   Home          seek to the start
#   Left/Right    -/+ 5 s (MP3) or previous/next frame (MP4)
#   Up/Down       volume
#   mouse wheel / click / drag on the timeline   seek
# Launch from the shell with:  import mplayer; mplayer.run(...) -- or from
# the file manager by opening a .mp3 / .mp4 file.

import os
import kern
import appbar
import text as textmod
from keyboard import Keyboard
import tkinter as tk
import mp3
import mp4

BG = (14, 17, 24)
PANEL = (22, 27, 38)
FG = (205, 214, 228)
DIM = (125, 136, 156)
ACCENT = (96, 155, 255)
GREEN_LO = (56, 118, 72)
GREEN_HI = (146, 226, 150)
GREY_BAR = (36, 42, 56)
HEAD_LINE = (70, 95, 140)
PLAYHEAD = (255, 255, 255)
STATUS_H = 15
TITLE_H = 16
MARGIN = 18
BAR_H = 64
TICK_MS = 250
SEEK_STEP = 5.0

_MEDIA_OK = True


def load_media(path):
    """Open path, decide MP3 vs MP4 from bytes, and return (kind, info,
    bars_or_frames).

    MP3: info gains "pcm"/"rate"/"channels" when the minimp3 C decoder
    is present (real decoded audio for the PC speaker).  MP4: info gains
    "_mp4" (the Mp4File) so the player can read_frame() payloads and
    actually display decodable streams (MJPEG / raw).
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] == b"ID3" or (len(data) > 3 and data[0] == 0xFF
                              and (data[1] & 0xE0) == 0xE0):
        info = mp3.get_info(data)
        bars = mp3.energy_map(data, 256)
        try:
            pcm = mp3.decode_pcm(data)
        except Exception as e:
            kern.write("mplayer: pcm decode failed: %s\n" % (e,))
            pcm = None
        if pcm:
            info["pcm"], info["rate"], info["channels"] = pcm
        else:
            info["pcm"] = None
        return "mp3", info, bars
    if data[:6] in (b"GIF87a", b"GIF89a"):
        import gif as gifmod
        g = gifmod.decode(data)
        delays = [d for (_img, d) in g.frames]
        avg = (sum(delays) / len(delays)) if delays else 100.0
        info = {
            "format": "GIF",
            "title": os.path.basename(path),
            "duration_s": sum(delays) / 1000.0,
            "width": g.width,
            "height": g.height,
            "codec": b"gif",
            "codec_name": "GIF animation",
            "fps": 1000.0 / avg if avg else 0.0,
            "frames": len(g.frames),
            "loop": g.loop,
        }
        return "gif", info, list(g.frames)
    if data[4:8] == b"ftyp":
        m = mp4.Mp4File(data)
        tr = m.video_track()
        # H.264/avc1 is decodable when the kernel's _h264 (h264bsd) module
        # is present; MJPEG/raw have pure-Python decoders; everything else
        # keeps the index + chart only.
        decodable = False
        if tr is not None:
            if tr["codec"] in (b"jpeg", b"raw "):
                decodable = True
            elif tr["codec"] == b"avc1":
                try:
                    import _h264
                    decodable = True
                except Exception:
                    decodable = False
        info = {
            "format": "MP4",
            "title": os.path.basename(path),
            "duration_s": m.duration_s if tr is None else tr["duration_s"],
            "width": tr["width"] if tr else 0,
            "height": tr["height"] if tr else 0,
            "codec": tr["codec"] if tr else b"",
            "codec_name": codec_name(tr["codec"]) if tr else "no video track",
            "fps": tr["fps"] if tr else 0.0,
            "frames": len(tr["frames"]) if tr else 0,
            "decodable": decodable,
            "_mp4": m,
        }
        frames = tr["frames"] if tr else []
        return "mp4", info, frames
    raise ValueError("mplayer: unsupported media format: %s" % path)


def codec_name(c):
    return {b"avc1": "H.264 (avc1)", b"mp4v": "MPEG-4 Part 2", b"jpeg":
            "MJPEG", b"raw ": "raw f(*)"}.get(c, c.decode('latin-1', 'replace'))


class Player(tk.Tk):
    """A full-screen media player: a widget-less Tk root that draws the
    waveform/frame chart and playhead in draw_region."""

    def __init__(self, kind, info, bars, path, caller_kb, on_quit):
        tk.Tk.__init__(self, title="mplayer: %s" % os.path.basename(path),
                       sidebar=True)
        self.kind = kind
        self.info = info
        self.bars = bars             # mp3: energy bars list ; mp4: frames list
        self.path = path
        self.caller_kb = caller_kb
        self.on_quit = on_quit
        self.bg = BG
        self.show_title = True
        self.playing = True
        self.pos_s = 0.0
        self.vol = 0.7
        self._seq = 0                # mp4: current frame index
        self._cum = None             # mp4: cumulative durations (ticks)
        self._tones = None           # mp3: [(freq, dur_ms), ...] tone track
        self._tone_t = 0.0           # mp3: cumulative seconds in tone track
        self._frame_cache = {}       # mp4: {index: png.Image} decoded frames
        if kind in ("mp4", "gif") and bars:
            self._build_frame_index()
        if kind == "mp3" and info.get("pcm"):
            self._tones = None   # built lazily on first play (fast UI)
            self._tone_pending = True
        self._dragging = False
        self._mouse_any = self._on_mouse_any
        # M9.7: real PCM playback state (AC'97, kern.audio_*).
        self._pcm_cursor = 0
        self._pcm_prev_pos = 0.0
        self._audio_done = False

    def _build_tone_track(self):
        """Build the square-wave tone track from the decoded PCM (M9.5
        sound out: the PC speaker is the only audio device, so the music
        is rendered as a pitch-following tone stream).  Falls back to a
        silent track when the estimation fails."""
        self._tone_pending = False
        try:
            tones = mp3.tone_track(self.info["pcm"], self.info["rate"],
                                   self.info["channels"])
        except Exception as e:
            kern.write("mplayer: tone track failed: %s\n" % (e,))
            tones = []
        self._tones = tones
        self._tone_t = 0.0
        if tones:
            self.info["tone_blocks"] = len(tones)
            kern.write("mplayer: audio ready (%d blocks)\n" % len(tones))
        else:
            kern.write("mplayer: no tone track\n")

    def _build_frame_index(self):
        tr = self.bars
        self._cum = []
        acc = 0
        for f in tr:
            if self.kind == "gif":
                acc += f[1]          # (png.Image, delay_ms)
            else:
                acc += f["duration"]
            self._cum.append(acc)
        if not self._cum:
            self._cum = []
            return
        if self.kind == "gif":
            self.info["timescale"] = 1000.0
            self.info["_ticks"] = acc
            return
        ts = self.info.get("timescale") or 1000
        self.info["timescale"] = ts
        self.info["_ticks"] = acc

    # ---- layout ------------------------------------------------------

    def _cavity(self, fb):
        return (appbar.SIDEBAR_W + MARGIN, TITLE_H + MARGIN,
                fb.width - appbar.SIDEBAR_W - appbar.right_sidebar_w()
                - 2 * MARGIN,
                fb.height - TITLE_H - STATUS_H - 2 * MARGIN)

    def _frac_of_x(self, fb, x):
        x0, y0, w, h = self._cavity(fb)
        if w <= 40:
            return 0.0
        return min(1.0, max(0.0, (x - x0) / float(w - 40)))

    def _timeline_y(self, cav_y, cav_h):
        """y of the timeline top inside the cavity (matches _draw_mp4: a
        decoded video frame area sits above the timeline for MP4)."""
        if (self.kind == "gif"
                or (self.kind == "mp4" and self.info.get("codec") in (b"jpeg", b"raw "))):
            vh = min(max(cav_h - 46 - BAR_H - 90, 40), 180)
            return cav_y + 60 + vh + 10
        return cav_y + 60

    # ---- playback state ----------------------------------------------

    def _duration(self):
        return self.info.get("duration_s", 0.0) or 0.0

    def _seek_frac(self, frac):
        dur = self._duration()
        self.pos_s = min(dur, max(0.0, frac * dur))
        self._sync_frame()
        self._reset_audio()
        self.redraw()

    def _sync_frame(self):
        if self.kind not in ("mp4", "gif") or not self._cum:
            return
        ticks = self.pos_s * self.info["timescale"]
        idx = 0
        for i, acc in enumerate(self._cum):
            if ticks < acc:
                idx = i
                break
            idx = i
        self._seq = idx

    def _seek(self, delta):
        dur = self._duration()
        self.pos_s = min(dur, max(0.0, self.pos_s + delta))
        if self.pos_s >= dur:
            self.pos_s = 0.0
        self._sync_frame()
        self._reset_audio()
        self.redraw()

    def _step_frame(self, d):
        if self.kind != "mp4" or not self._cum:
            self._seek(SEEK_STEP * d)
            return
        n = len(self._cum)
        i = max(0, min(n - 1, self._seq + d))
        self._seq = i
        self.pos_s = self._cum[i] / float(self.info["timescale"])
        self._reset_audio()
        self.redraw()

    def play_pause(self):
        self.playing = not self.playing
        if not self.playing:
            try:
                kern.audio_stop()
            except Exception:
                pass
        self.redraw()

    def _reset_audio(self):
        """Rewind the PCM cursor and stop AC'97 playback (seek / wrap)."""
        self._pcm_cursor = 0
        self._audio_done = False
        try:
            kern.audio_stop()
        except Exception:
            pass

    def _advance(self):
        if self.destroyed:
            return
        import tk as tkmod
        if tkmod._ACTIVE_ROOT is not self:
            return
        if self.playing:
            dur = self._duration()
            self.pos_s = (self.pos_s + TICK_MS / 1000.0) % dur if dur else 0.0
            self._sync_frame()
            self._audio_sync()
            self.redraw()
        else:
            # keep audio silent while paused (a seek while paused
            # still resyncs on the next play)
            self._audio_sync()
        kern.after(TICK_MS, self._advance)

    # ---- M9.5: PC speaker sound out (MP3) ----------------------------

    def _speaker_sync(self):
        """Drive the PC speaker from the tone track at the playhead.

        kern.speaker(hz) programs PIT channel 2 (square wave); 0 silences.
        Only active while playing and above the mute threshold; the volume
        control scales the tone on/off duty so 0% is truly silent.  The
        tone track is built lazily on the first play so the UI appears
        instantly."""
        if getattr(self, "_tone_pending", False):
            self._build_tone_track()
        tones = self._tones
        if tones is None:
            return
        try:
            import kern as _k
            if not self.playing or (self.vol or 0) <= 0.02:
                _k.speaker_off()
                return
            # find the tone block covering the playhead
            t = self.pos_s
            acc = 0.0
            for freq, dur_ms in tones:
                nxt = acc + dur_ms / 1000.0
                if t < nxt:
                    if freq:
                        _k.speaker(freq)
                    else:
                        _k.speaker_off()
                    return
                acc = nxt
            _k.speaker_off()
        except Exception:
            pass

    # ---- M9.7: real PCM audio out (AC'97) ---------------------------

    def _audio_sync(self):
        """Stream the decoded PCM through the AC'97 device at the playhead.

        Falls back to the PC-speaker tone rendition (M9.5) when no audio
        device is present.  The cursor tracks bytes already pushed, so a
        pause/seek/wrap resyncs cleanly; volume scales in the kernel."""
        try:
            if not kern.audio_ready():
                return self._speaker_sync()
        except Exception:
            return self._speaker_sync()
        pcm = self.info.get("pcm")
        if not pcm:
            return
        rate = self.info.get("rate") or 0
        ch = self.info.get("channels") or 0
        if rate <= 0 or ch <= 0:
            return
        try:
            # wrap detection: playhead went backwards -> rewind the cursor
            if self.pos_s < self._pcm_prev_pos:
                self._pcm_cursor = 0
                self._audio_done = False
                kern.audio_stop()
            self._pcm_prev_pos = self.pos_s
            if not self.playing or (self.vol or 0) <= 0.02:
                kern.audio_stop()
                return
            total = min(len(pcm), int(self.pos_s * rate * ch * 2))
            if total <= self._pcm_cursor:
                if self._audio_done:
                    kern.audio_stop()
                return
            chunk = pcm[self._pcm_cursor:total]
            n = kern.audio_play(chunk, rate, ch, int((self.vol or 0.7) * 128))
            if n:
                self._pcm_cursor += n
            if self._pcm_cursor >= len(pcm):
                self._audio_done = True
        except Exception:
            pass

    # ---- drawing -----------------------------------------------------

    def draw_region(self, fb, ox, oy, rw, rh):
        buffered, target = self._begin_shadow(fb, ox, oy, rw, rh)
        if target is None:
            return
        sx, sxr, y = self._draw_app_frame(target, ox, oy, rw, rh)
        cav_x, cav_y, cav_w, cav_h = self._cavity(fb)
        if self.kind == "mp3":
            self._draw_mp3(target, cav_x, cav_y, cav_w, cav_h)
        else:
            self._draw_mp4(target, cav_x, cav_y, cav_w, cav_h)
        # volume meter (top right of the info area)
        self._draw_volume(target, cav_x + cav_w - 90, cav_y + 34, 84)
        # status bar
        sy = fb.height - STATUS_H
        target.fill_rect(sx, sy, fb.width - sx - sxr, STATUS_H, 10, 13, 18)
        hint = ("[Space play/pause  |  Left/Right ±5s  |  Up/Down vol  |  "
                "wheel/click seek  |  Home start  |  Esc quit]")
        if self.kind == "mp4":
            hint = ("[Space play/pause  |  Left/Right frame  |  Up/Down vol"
                    "  |  wheel/click seek  |  Esc quit]")
        textmod.draw_text(target, sx + 6, sy + 3, hint, (130, 140, 158))
        t = mp3.fmt_time(self.pos_s)
        textmod.draw_text(target, fb.width - sxr - 96, sy + 3,
                          "%s / %s" % (t, mp3.fmt_time(self._duration())),
                          (200, 210, 225))
        self._end_shadow(fb, target, buffered)

    def _panel(self, target, x, y, w, h):
        target.fill_rect(x, y, w, h, *PANEL)
        for yy in (y, y + h - 1):
            target.fill_rect(x, yy, w, 1, 44, 52, 70)

    def _draw_mp3(self, target, x, y0, w, h):
        info = self.info
        # header block
        self._panel(target, x, y0, w, 46)
        title = info.get("title") or os.path.basename(self.path)
        textmod.draw_text(target, x + 10, y0 + 7, title, ACCENT)
        sub = []
        if info.get("artist"):
            sub.append(info["artist"])
        if info.get("album"):
            sub.append(info["album"])
        if info.get("year"):
            sub.append(info["year"])
        textmod.draw_text(target, x + 10, y0 + 21, "  \xb7  ".join(sub), DIM)
        textmod.draw_text(target, x + 10, y0 + 33,
                          "%s  %d kbps  %d Hz  %s"
                          % (mp3.fmt_time(self._duration()),
                             info.get("bitrate", 0) // 1000,
                             info.get("samplerate", 0),
                             info.get("channels_str", "")), FG)
        # waveform timeline
        bars = self.bars
        bx, by = x + 20, y0 + 60
        bw, bh = w - 40, BAR_H
        self._panel(target, bx, by, bw, bh)
        dur = self._duration()
        n = len(bars)
        if n:
            step = bw / float(n)
            pos = self.pos_s / dur if dur else 0.0
            frac_dim = int(n * pos)
            for i, e in enumerate(bars):
                bhgt = max(2, int(e * (bh - 8)))
                col = GREEN_HI if i <= frac_dim else GREY_BAR
                if i <= frac_dim:
                    col = GREEN_LO if e < 0.45 else GREEN_HI
                px = bx + int(i * step)
                target.fill_rect(px, by + bh - bhgt, max(1, int(step) - 1),
                                 bhgt, *col)
        # playhead
        px = bx + int((bw - 1) * (self.pos_s / dur if dur else 0.0))
        target.fill_rect(px, by, 1, bh, *PLAYHEAD)
        return

    def _draw_mp4(self, target, x, y0, w, h):
        info = self.info
        self._panel(target, x, y0, w, 46)
        textmod.draw_text(target, x + 10, y0 + 7,
                          info.get("title") or os.path.basename(self.path),
                          ACCENT)
        textmod.draw_text(target, x + 10, y0 + 21,
                          "%dx%d  %s  %.2f fps  %d frames"
                          % (info.get("width", 0), info.get("height", 0),
                             info.get("codec_name", ""),
                             info.get("fps", 0.0), info.get("frames", 0)),
                          DIM)
        # Display the current video frame when the codec is decodable:
        # MJPEG via jpeg.py, raw RGB direct, H.264/avc1 via the _h264 C
        # module (h264bsd, baseline profile).  Everything else keeps the
        # index + chart only.
        decodable = (self.kind == "gif"
                     or info.get("codec") in (b"jpeg", b"raw ")
                     or (info.get("codec") == b"avc1"
                         and info.get("decodable")))
        note = ("frame %d/%d  %s" % (self._seq + 1, info.get("frames", 0),
                                     info.get("codec_name", "")))
        if not decodable:
            note = ("Track index: decoding %s is not supported here "
                    "(need H.264 baseline)" % info.get("codec_name", ""))
        textmod.draw_text(target, x + 10, y0 + 33, note, FG)
        # video area (decoded frame) above the timeline
        frames = self.bars
        bx, by = x + 20, y0 + 60
        bw = w - 40
        vh = 46
        if decodable and frames:
            vh = min(max(h - 46 - BAR_H - 90, 40), 180)
        vx, vy = bx, by
        self._panel(target, vx, vy, bw, vh)
        if decodable and frames:
            img = self._current_frame_image()
            if img is not None:
                # scale to fit the video area (nearest neighbour)
                s = min(bw / float(img.w), vh / float(img.h))
                s = max(0.05, min(1.0, s))
                ow, oh = max(1, int(img.w * s)), max(1, int(img.h * s))
                ox = vx + (bw - ow) // 2
                oy = vy + (vh - oh) // 2
                try:
                    img.blit(target, ox, oy, s)
                except Exception:
                    pass
        # frame size chart / timeline
        cy = vy + vh + 10
        self._panel(target, bx, cy, bw, BAR_H)
        if frames:
            n = len(frames)
            step = bw / float(n)
            if self.kind == "gif":
                # GIF frames are (png.Image, delay_ms) tuples: draw a simple
                # uniform timeline instead of the MP4 size chart.
                for i in range(n):
                    bhgt = 10
                    col = GREEN_HI if i % 2 == 0 else GREY_BAR
                    px = bx + int(i * step)
                    target.fill_rect(px, cy + BAR_H - bhgt,
                                     max(1, int(step) - 1), bhgt, *col)
            else:
                mx = max((f["size"] for f in frames), default=1) or 1
                ts = self.info["timescale"]
                pos = self.pos_s * ts
                for i, f in enumerate(frames):
                    bhgt = max(2, int((f["size"] / float(mx)) * (BAR_H - 8)))
                    if f["duration"] >= 2:
                        col = GREEN_HI      # large (I) frame
                    else:
                        col = (64, 92, 148) if f["size"] > mx * 0.4 else GREY_BAR
                    px = bx + int(i * step)
                    target.fill_rect(px, cy + BAR_H - bhgt,
                                     max(1, int(step) - 1), bhgt, *col)
        px = bx + int((bw - 1) * (self.pos_s / self._duration()
                                  if self._duration() else 0.0))
        target.fill_rect(px, cy, 1, BAR_H, *PLAYHEAD)
        return

    def _current_frame_image(self):
        """Decode (and cache) the png.Image for the current MP4 frame.

        MJPEG: jpeg.decode_jpeg; raw: wrap the RGB payload directly.
        Returns None when the payload is not decodable."""
        if self.kind == "gif":
            frames = self.bars
            if frames and 0 <= self._seq < len(frames):
                return frames[self._seq][0]
            return None
        frames = self.bars
        if not frames:
            return None
        idx = self._seq
        if idx in self._frame_cache:
            return self._frame_cache[idx]
        m = self.info.get("_mp4")
        if m is None:
            return None
        try:
            payload = m.read_frame(idx)
            codec = self.info.get("codec")
            if codec == b"raw ":
                import png as pngmod
                w = int(self.info.get("width") or 0)
                h = int(self.info.get("height") or 0)
                if w <= 0 or h <= 0 or len(payload) < w * h * 3:
                    return None
                img = pngmod.Image(w, h, "rgb")
                img.pixels[:] = payload[:w * h * 3]
            elif codec == b"jpeg":
                import jpeg as jpegmod
                img = jpegmod.decode_jpeg(payload)
            elif codec == b"avc1":
                # H.264 baseline via the _h264 C module (h264bsd).  Samples
                # are AVCC (length-prefixed NALs); rebuild an Annex-B access
                # unit = SPS+PPS (from avcC) + the sample's NALs with start
                # codes.  The module keeps the decoder state (SPS/PPS,
                # reference frames) across calls, so P-frames decode too.
                import _h264 as h264mod
                import png as pngmod
                m = self.info.get("_mp4")
                tr = m.video_track()
                nal_len = tr.get("nal_len", 4)
                aus = bytearray()
                for s in tr.get("sps", ()) or ():
                    aus += b"\x00\x00\x00\x01" + s
                for p in tr.get("pps", ()) or ():
                    aus += b"\x00\x00\x00\x01" + p
                q = 0
                while q + nal_len <= len(payload):
                    ln = int.from_bytes(payload[q:q + nal_len], "big")
                    q += nal_len
                    if ln == 0 or q + ln > len(payload):
                        break
                    aus += b"\x00\x00\x00\x01" + payload[q:q + ln]
                    q += ln
                dec = h264mod.decode(bytes(aus))
                if dec is None:
                    return None
                cw, ch, yuv = dec
                dw = int(self.info.get("width") or cw)
                dh = int(self.info.get("height") or ch)
                rgb = h264mod.to_rgb(yuv, cw, ch, dw, dh)
                img = pngmod.Image(dw, dh, "rgb")
                img.pixels[:] = rgb
            else:
                return None
        except Exception:
            return None
        if len(self._frame_cache) > 12:
            # keep the cache small (each frame can be 100s of KB)
            self._frame_cache.clear()
        self._frame_cache[idx] = img
        return img

    def _draw_volume(self, target, x, y, w):
        target.fill_rect(x, y, w, 5, 30, 36, 48)
        v = max(0, min(1, self.vol or 0))
        fillw = max(1, int(w * v))
        col = (146, 226, 150) if v > 0.5 else ((226, 200, 140) if v > 0.2
                                               else (220, 120, 110))
        target.fill_rect(x, y, fillw, 5, *col)
        textmod.draw_text(target, x, y + 7, "vol %3d%%" %
                          int(v * 100), DIM)

    # ---- mouse (widget-less root) -----------------------------------

    def _on_mouse_any(self, ev):
        x, y, button, pressed, dbl, clicks, wheel = ev
        if wheel:
            self._seek(SEEK_STEP * (1 if wheel > 0 else -1))
            return
        if pressed and button == 1:
            cav_x, cav_y, cav_w, cav_h = self._cavity(self.fb)
            tx = cav_x + 20
            ty = self._timeline_y(cav_y, cav_h)
            tw = max(1, cav_w - 40)
            if ty <= y < ty + BAR_H:
                self._dragging = True
                frac = min(1.0, max(0.0, (x - tx) / float(tw)))
                self._seek_frac(frac)
                return
            self._seek_frac(self._frac_of_x(self.fb, x))
            return
        if button == 1 and not pressed:
            self._dragging = False
            return
        if button == 0 and not pressed and self._dragging:
            self._seek_frac(self._frac_of_x(self.fb, x))

    def on_key(self, ev):
        if self.destroyed:
            return
        name, ch, pressed = ev
        if not pressed:
            return
        if name == 'esc' or ch in (ord('q'), ord('Q')):
            self._quit()
            return
        if name == 'space':
            self.play_pause()
            return
        if name == 'home':
            self._seek_frac(0.0)
            return
        if name == 'left':
            self._step_frame(-1)
            return
        if name == 'right':
            self._step_frame(1)
            return
        if name == 'up':
            self.vol = min(1.0, (self.vol or 0) + 0.1)
            self.redraw()
            return
        if name == 'down':
            self.vol = max(0.0, (self.vol or 0) - 0.1)
            self.redraw()
            return
        tk.Tk.on_key(self, ev)

    def _quit(self):
        if self.destroyed:
            return
        try:
            kern.speaker_off()
        except Exception:
            pass
        try:
            kern.audio_stop()
        except Exception:
            pass
        try:
            import _h264
            _h264.close()
        except Exception:
            pass
        kern.write("mplayer: quit\n")
        try:
            self.caller_kb.activate()
        except AttributeError:
            kern.on_key(self.caller_kb._dispatch)
        self.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    """Start the player.  kb is the CALLER keyboard (restored on quit);
    returns immediately (the kernel event loop drives the UI)."""
    if path is None:
        raise ValueError("mplayer: a file path is required")
    kern.write("mplayer: loading %s\n" % path)
    kind, info, bars = load_media(path)
    kern.write("mplayer: %s %s\n" % (kind.upper(),
                                     mp3.fmt_time(info.get("duration_s", 0))))
    kb2 = Keyboard()
    root = Player(kind, info, bars, path, kb, on_quit)
    root._prev_kb = kb
    root._back = on_quit
    root._close_handler = root._quit
    root.start(fb, kb2, mouse)
    # gate marker: the first redraw has completed (MP4: first frame decoded
    # and drawn when decodable; MP3: waveform/panel on screen).  The media
    # kind is part of the marker so a gate can wait for the exact screen
    # (the accumulated serial log would otherwise match a stale marker).
    kern.write("mplayer: %s screen ready\n" % kind.upper())
    root._advance()
    root.mainloop()
    kern.write("mplayer: player ready (event-driven)\n")