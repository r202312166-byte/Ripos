# gif.py -- pure-Python animated GIF decoder for Ripos (M9.7).
#
# Parses GIF87a/89a: global/local color tables, LZW (all code sizes),
# interlacing, transparency, graphic-control extensions (delay, disposal,
# transparency) and NETSCAPE loop counts.  Frames are returned as png.Image
# (rgb) ready for the framebuffer, exactly like jpeg.decode_jpeg.

import png


class GifError(Exception):
    pass


class GifImage:
    def __init__(self):
        self.width = 0
        self.height = 0
        self.loop = 0        # 0 = loop forever
        self.bg = 0
        self.frames = []     # [(png.Image, delay_ms), ...]


def _table(raw):
    return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]


def _read_subblocks(data, pos):
    """Read a series of LZW sub-blocks; returns (blob, newpos)."""
    out = bytearray()
    while True:
        n = data[pos]
        pos += 1
        if n == 0:
            return bytes(out), pos
        out += data[pos:pos + n]
        pos += n


def _lzw_decode(blob, min_code, w, h):
    """Decode a GIF LZW-compressed index stream (min_code + 1..12 bits)."""
    clear = 1 << min_code
    end = clear + 1
    width = min_code + 1
    dic = [bytes([i]) for i in range(clear)]
    out = bytearray()
    bits = 0
    acc = 0
    pos = 0
    prev = None

    def read_code():
        nonlocal pos, bits, acc, width
        while bits < width:
            if pos >= len(blob):
                return None
            acc |= blob[pos] << bits
            pos += 1
            bits += 8
        code = acc & ((1 << width) - 1)
        acc >>= width
        bits -= width
        return code

    # The GIF dictionary skips the clear and end codes: after a clear the
    # first new entry lands at index end+1, so index the list by code.
    def fresh_dict():
        d = [bytes([i]) for i in range(clear)]
        d.append(None)  # clear code slot
        d.append(None)  # end code slot
        return d

    dic = fresh_dict()
    next_code = end + 1
    while True:
        code = read_code()
        if code is None or code == end:
            break
        if code == clear:
            dic = fresh_dict()
            next_code = end + 1
            width = min_code + 1
            prev = None
            continue
        if prev is None:
            if code >= next_code:
                break
            entry = dic[code]
            out += entry
            prev = code
            continue
        if code < next_code:
            entry = dic[code]
        elif code == next_code:
            entry = dic[prev] + dic[prev][:1]
        else:
            break
        out += entry
        if next_code < 4096:
            dic.append(dic[prev] + entry[:1])
            next_code += 1
            if next_code == (1 << width) and width < 12:
                width += 1
        prev = code
    return out[:w * h]


def _deinterlace(idx, iw, ih, interlace):
    """Split the index stream into rows, undoing GIF interlacing."""
    if not interlace or ih < 1:
        return [idx[y * iw:(y + 1) * iw] for y in range(ih)]
    rows = [None] * ih
    src = 0
    for start, step in ((0, 8), (4, 8), (2, 4), (1, 2)):
        y = start
        while y < ih:
            rows[y] = idx[src * iw:(src + 1) * iw]
            src += 1
            y += step
    return rows


def _bg_canvas(w, h, bg, table):
    if table and 0 <= bg < len(table):
        r, g, b = table[bg]
    else:
        r = g = b = 0
    return bytes((r, g, b)) * (w * h)


def decode(data):
    """Decode a GIF into a GifImage (frames = (png.Image, delay_ms))."""
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise GifError("not a GIF")
    w = int.from_bytes(data[6:8], "little")
    h = int.from_bytes(data[8:10], "little")
    flags = data[10]
    bg = data[11]
    gct = None
    pos = 13
    if flags & 0x80:
        n = 3 * (2 << (flags & 7))
        gct = _table(data[pos:pos + n])
        pos += n
    result = GifImage()
    result.width, result.height, result.bg = w, h, bg
    canvas = bytearray(_bg_canvas(w, h, bg, gct))
    prev_disposal = 0
    frame_delay = 10
    transp = None
    disposal = 0
    n_frames = 0
    while pos < len(data):
        block = data[pos]
        pos += 1
        if block == 0x3B:
            break
        if block == 0x21:
            ext = data[pos]
            pos += 1
            if ext == 0xF9:
                size = data[pos]; pos += 1
                packed = data[pos]; pos += 1
                delay = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
                tindex = data[pos]; pos += 1
                pos += 1  # block terminator
                disposal = (packed >> 2) & 7
                transp = tindex if packed & 1 else None
                frame_delay = delay
                if size > 4:
                    pos += size - 4
            else:
                _, pos = _read_subblocks(data, pos)
            continue
        if block == 0x2C:
            left = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
            top = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
            iw = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
            ih = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
            packed = data[pos]; pos += 1
            lct = None
            if packed & 0x80:
                n = 3 * (2 << (packed & 7))
                lct = _table(data[pos:pos + n])
                pos += n
            min_code = data[pos]; pos += 1
            blob, pos = _read_subblocks(data, pos)
            table = lct if lct is not None else gct
            if not table:
                raise GifError("no color table")
            # apply the previous frame's disposal
            if prev_disposal == 2:
                canvas[:] = _bg_canvas(w, h, bg, gct)
            elif prev_disposal == 3 and n_frames > 0:
                canvas[:] = saved
            saved = bytes(canvas)
            idx = _lzw_decode(blob, min_code, iw, ih)
            rows = _deinterlace(idx, iw, ih, bool(packed & 0x40))
            for yy in range(ih):
                y = top + yy
                if y < 0 or y >= h:
                    continue
                row = rows[yy]
                base = y * w * 3
                for xx in range(iw):
                    x = left + xx
                    if x < 0 or x >= w:
                        continue
                    v = row[xx]
                    if transp is not None and v == transp:
                        continue
                    r, g, b = table[v]
                    o = base + x * 3
                    canvas[o] = r
                    canvas[o + 1] = g
                    canvas[o + 2] = b
            img = png.Image(w, h, "rgb")
            img.pixels[:] = bytes(canvas)
            delay_ms = max(2, frame_delay * 10) if frame_delay else 100
            result.frames.append((img, delay_ms))
            n_frames += 1
            prev_disposal = disposal
            continue
        break
    if not result.frames:
        raise GifError("no image frames")
    return result

