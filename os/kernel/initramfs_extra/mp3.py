# mp3.py -- minimal pure-Python MP3 codec for Ripos (M9.5).
#
# Parses ID3v2/ID3v1 tags and the MPEG audio frame headers so a player can
# show tags, duration, bitrate, sample rate and channels -- and renders a
# waveform/spectrum-style energy envelope by sampling the frame payloads.
#
# M9.5 Phase 1+2: REAL sound out.  The kernel now carries a vendored
# minimp3 C decoder as the builtin `_mp3dec` module (mp3decmod.c):
# decode_pcm() turns a whole MP3 buffer into 16-bit PCM, and the player
# (mplayer.py) plays that PCM through the PC speaker (kern.speaker) as a
# square-wave rendition -- the only sound device this OS has.  The energy
# envelope path stays for the visualization and as a fallback when the
# C module is absent (host tests).

class Mp3Error(Exception):
    pass


# MPEG1 Layer III bitrate table (kbps) by index (0=free).
_BR_L3_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BR_L3_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_BR_L2_V1 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)
_BR_L2_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_BR_L1_V1 = (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448)
_BR_L1_V2 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256)

_SR_V1 = (44100, 48000, 32000)
_SR_V2 = (22050, 24000, 16000)
_SR_V25 = (11025, 12000, 8000)

_VERSIONS = {3: "MPEG1", 2: "MPEG2", 0: "MPEG2.5"}
_LAYERS = {3: 1, 2: 2, 1: 3}
_CHANNELS = {0: "stereo", 1: "joint stereo", 2: "dual channel", 3: "mono"}


def _syncsafe(b, o):
    return ((b[o] & 0x7F) << 21) | ((b[o + 1] & 0x7F) << 14) \
        | ((b[o + 2] & 0x7F) << 7) | (b[o + 3] & 0x7F)


def parse_id3v2(data):
    """Parse the leading ID3v2 tag (if any) into a dict of text frames."""
    if not data.startswith(b"ID3") or len(data) < 10:
        return None
    ver = data[3]
    size = _syncsafe(data, 6)
    end = min(len(data), 10 + size)
    frames = {}
    pos = 10
    flags = data[5]
    ext = 0
    if flags & 0x40 and pos + 4 <= end:   # extended header
        ext = _syncsafe(data, pos)
        pos += 4 + ext
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if fid[0] == 0:   # padding
            break
        if ver >= 4:
            fsize = _syncsafe(data, pos + 4)
            fhdr = 10
        else:
            fsize = int.from_bytes(data[pos + 4:pos + 8], "big")
            fhdr = 10 if ver >= 3 else 6   # v2.2: 4+3; v2.3/2.4: 4+4+2
        flags2 = data[pos + 8:pos + 10] if ver >= 3 else b""
        if fsize <= 0 or pos + fhdr + fsize > end:
            break
        payload = data[pos + fhdr:pos + fhdr + fsize]
        # skip compressed/encrypted frames (flag bits 0x80/0x40 for v2.3)
        if ver >= 3 and (flags2[0] & 0xC0 or flags2[1] & 0x80):
            pos += fhdr + fsize
            continue
        if fid[:1] == b"T":
            enc = payload[0] if payload else 0
            raw = payload[1:]
            if enc == 0:
                val = raw.decode("latin-1", "replace")
            elif enc == 3:
                val = raw.decode("utf-8", "replace")
            else:
                bom = raw[:2]
                body = raw[2:] if bom in (b"\xff\xfe", b"\xfe\xff") else raw
                val = body.decode("utf-16", "replace")
            val = val.replace("\x00", "").strip()
            if val:
                key = fid.decode("latin-1")
                frames.setdefault(key, val)
        pos += fhdr + fsize
    return frames


def parse_id3v1(data):
    """Parse the trailing 128-byte ID3v1 tag (if any)."""
    if len(data) < 128 or data[-128:-125] != b"TAG":
        return None
    chunk = data[-125:-1]
    title = chunk[0:30].split(b"\x00")[0]
    artist = chunk[30:60].split(b"\x00")[0]
    album = chunk[60:90].split(b"\x00")[0]
    year = chunk[90:94].split(b"\x00")[0]
    try:
        title = title.decode("latin-1").strip()
        artist = artist.decode("latin-1").strip()
        album = album.decode("latin-1").strip()
        year = year.decode("latin-1").strip()
    except UnicodeDecodeError:
        pass
    return {"title": title, "artist": artist, "album": album, "year": year}


def _frame_header(data, pos):
    """Parse one MPEG frame header at pos (already sync-checked)."""
    b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
    version = (b1 >> 3) & 0x3      # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer_field = (b1 >> 1) & 0x3  # 3=Layer I, 2=Layer II, 1=Layer III
    br_idx = (b2 >> 4) & 0xF
    sr_idx = (b2 >> 2) & 0x3
    padding = (b2 >> 1) & 0x1
    mode = (b3 >> 6) & 0x3
    if layer_field == 0 or sr_idx == 3 or br_idx == 15:
        return None
    if version == 1:   # reserved
        return None
    layer = _LAYERS[layer_field]
    sr_tab = _SR_V1 if version == 3 else (_SR_V2 if version == 2 else _SR_V25)
    sr = sr_tab[sr_idx]
    if layer == 1:   # Layer I
        br = _BR_L1_V1 if version == 3 else _BR_L1_V2
    elif layer == 2: # Layer II
        br = _BR_L2_V1 if version == 3 else _BR_L2_V2
    else:            # Layer III
        br = _BR_L3_V1 if version == 3 else _BR_L3_V2
    bitrate = br[br_idx] * 1000
    if bitrate == 0:
        return None
    if layer == 1:
        frame_len = (12 * bitrate // sr + padding) * 4
        samples = 384
    elif layer == 2:
        frame_len = 144 * bitrate // sr + padding
        samples = 1152
    else:
        if version == 3:
            frame_len = 144 * bitrate // sr + padding
            samples = 1152
        else:
            frame_len = 72 * bitrate // sr + padding
            samples = 576
    return {
        "offset": pos,
        "version": _VERSIONS.get(version, "?"),
        "layer": layer,
        "samples": samples,
        "bitrate": bitrate,
        "samplerate": sr,
        "padding": padding,
        "channels": 1 if mode == 3 else 2,
        "mode": _CHANNELS[mode],
        "len": frame_len,
        "data": pos + 4,
    }


def iter_frames(data, start=0):
    """Yield _frame_header dicts for every playable frame, starting after
    the ID3v2 tag (or at start)."""
    n = len(data)
    pos = start
    i = start
    limit = n - 4
    while i < limit:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            h = _frame_header(data, i)
            if h is not None and i + h["len"] <= n:
                yield h
                i += h["len"]
                pos = i
                continue
        i += 1
    return pos


def get_info(data):
    """Return a dict of everything a player needs to display a file."""
    info = {
        "format": "MP3", "title": "", "artist": "", "album": "",
        "year": "", "duration_s": 0.0, "bitrate": 0, "samplerate": 0,
        "channels": 0, "frames": 0, "size": len(data), "audio_start": 0,
    }
    tag = parse_id3v2(data)
    start = 0
    if tag:
        start = 10 + _syncsafe(data, 6)
        info["title"] = tag.get("TIT2", "")
        info["artist"] = tag.get("TPE1", "")
        info["album"] = tag.get("TALB", "")
        info["year"] = tag.get("TYER", "")
    info["audio_start"] = start
    frames = list(iter_frames(data, start))
    if not frames:
        v1 = parse_id3v1(data)
        if v1:
            info.update(v1)
        raise Mp3Error("no playable MPEG frames found")
    hdr = frames[0]
    total = sum(f["samples"] for f in frames)
    info["samplerate"] = hdr["samplerate"]
    info["channels"] = hdr["channels"]
    info["channels_str"] = hdr["mode"]
    info["bitrate"] = sum(f["bitrate"] for f in frames) // len(frames)
    info["frames"] = len(frames)
    info["duration_s"] = total / float(hdr["samplerate"])
    if not info.get("year"):
        v1 = parse_id3v1(data)
        if v1:
            info["year"] = v1.get("year", "")
            if not info["title"]:
                info["title"] = v1.get("title", "")
            if not info["artist"]:
                info["artist"] = v1.get("artist", "")
            if not info["album"]:
                info["album"] = v1.get("album", "")
    return info


def energy_map(data, width, start=0, end=None):
    """Sample the MPEG payloads into `width` energy bars (0.0..1.0), one per
    bar.  Each bar averages the |byte-128| deviation over its slice of the
    audio span -- a cheap proxy for loudness that makes a nice visualizer
    even without a full Huffman decode."""
    if end is None or end > len(data):
        end = len(data)
    total = end - start
    if total <= 0 or width <= 0:
        return [0.0] * width
    bars = [0.0] * width
    if total < width:
        # pad small files with silence
        seg = max(1, total // max(1, width // 2))
        count = 0
        i = start
        while i < end:
            b = 0
            acc = 0
            while b < seg and i < end:
                v = data[i] - 128
                acc += v if v >= 0 else -v
                b += 1
                i += 1
            count += 1
            bars[count - 1] = acc / (b * 128.0)
        return bars
    step = max(1, total // width)
    for k in range(width):
        s = start + k * step
        e = min(start + (k + 1) * step, end)
        acc = 0
        m = e - s
        if m > 0:
            for j in range(s, e):
                v = data[j] - 128
                acc += v if v >= 0 else -v
            bars[k] = acc / (m * 128.0)
    return bars


def fmt_time(seconds):
    m, s = divmod(int(seconds), 60)
    h, s = divmod(s, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


# ---- M9.5: real PCM decode + square-wave tone track --------------------

_mp3dec = None


def _load_mp3dec():
    """Lazily import the kernel's builtin minimp3 module (absent on the
    host test runner, where the C module is not linked)."""
    global _mp3dec
    if _mp3dec is None:
        try:
            import _mp3dec as m
            _mp3dec = m
        except ImportError:
            _mp3dec = False
    return _mp3dec or None


def decode_pcm(data):
    """Decode a whole MP3 buffer to 16-bit little-endian interleaved PCM.

    Returns (pcm_bytes, sample_rate, channels) or None when the minimp3 C
    module is unavailable.  The C module raises ValueError on undecodable
    input; callers should treat that as 'no audio'."""
    m = _load_mp3dec()
    if m is None:
        return None
    return m.decode(data)


def _mono8k(pcm, rate, channels):
    """Downmix to mono and decimate toward 8 kHz for the tone tracker.

    Uses the C module's fast path in the kernel; falls back to a pure-
    Python pass on the host test runner."""
    m = _load_mp3dec()
    if m is not None and hasattr(m, "mono8k"):
        return m.mono8k(pcm, rate, channels)
    # pure-Python fallback (host): average channels, sample every kth frame
    import array
    a = array.array('h')
    a.frombytes(pcm)
    n = len(a)
    frames = n // channels
    out_rate = min(rate or 8000, 8000)
    step = max(1, (rate or 8000) // out_rate)
    out = bytearray((frames // step) * 2)
    k = 0
    for i in range(0, frames - channels, step):
        s = a[i * channels]
        if channels == 2:
            s = (s + a[i * channels + 1]) >> 1
        out[k] = s & 0xFF
        out[k + 1] = (s >> 8) & 0xFF
        k += 2
    return bytes(out), out_rate


def tone_track(pcm, sample_rate, channels, block=1152, lo=55, hi=2400):
    """Derive a square-wave 'melody' from the decoded PCM.

    Each block of `block` mono samples (one MPEG frame at 44.1 kHz)
    becomes a tone: the dominant frequency is estimated by zero crossings
    (a crude but robust pitch tracker for a square-wave speaker), gated by
    the block RMS so silence stays silent.  Returns a list of
    (freq_hz_or_0, duration_ms) tuples that a player can feed to
    kern.speaker().  Runs on an 8 kHz mono stream (C-decimated in the
    kernel) so the pure-Python pass is fast."""
    import array
    mono, sr = _mono8k(pcm, sample_rate, channels)
    a = array.array('h')
    a.frombytes(mono)
    n = len(a)
    out = []
    i = 0
    while i < n:
        blk = a[i:i + block]
        i += block
        m = len(blk)
        if m < 4:
            continue
        # RMS loudness (every 2nd sample is enough for a gate).
        acc = 0
        cnt = 0
        j = 0
        while j < m:
            s = blk[j]
            acc += s * s
            cnt += 1
            j += 2
        rms = (acc / float(cnt)) ** 0.5
        # Zero crossings (every 2nd sample).
        zc = 0
        prev = blk[0]
        j = 1
        while j < m:
            s = blk[j]
            if (prev < 0) != (s < 0):
                zc += 1
            prev = s
            j += 2
        dur_ms = m * 1000.0 / sr if sr else 0
        if rms < 120 or zc == 0:
            out.append((0, int(dur_ms)))
            continue
        f = zc * sr / (2.0 * m)
        f = max(lo, min(hi, f))
        # Quantize to a coarse grid so consecutive blocks share tones.
        f = int(round(f / 25.0) * 25)
        out.append((int(f), int(dur_ms)))
    return out

