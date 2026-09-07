# mp4.py -- minimal pure-Python MP4 / ISO-BMFF parser for Ripos (M9.5).
#
# Parses the atom/box tree and the sample tables of a video track
# (moov/trak/mdia/minf/stbl: stsd, stts, stsz, stsc, stco/co64) so that
# metadata (duration, dimensions, codec, frame rate) and the frame index
# (offset + size + duration per sample) are available without any C code.
# Frame payloads can be read straight out of the file (mdat), so playback
# of MJPEG / raw / any intra-coded stream is a matter of feeding the
# decoder.
#
# API:  Mp4File(data)  ->  .duration_s  .video_track (dict)  .frames(track)
#                            .read_frame(track, index) -> bytes

import struct


class Mp4Error(Exception):
    pass


def _u32(b, o):
    return struct.unpack(">I", b[o:o + 4])[0]


def _u64(b, o):
    return struct.unpack(">Q", b[o:o + 8])[0]


def _s16_16(b, o):
    return _u32(b, o) / 65536.0


def _iter_boxes(data, start, end):
    """Yield (type, payload_start, payload_end, header_end) for each box."""
    pos = start
    while pos + 8 <= end:
        size = _u32(data, pos)
        btype = data[pos + 4:pos + 8]
        hdr = 8
        if size == 1:
            size = _u64(data, pos + 8)
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            raise Mp4Error("bad box size %d at %d" % (size, pos))
        yield btype, pos + hdr, pos + size, pos + hdr
        pos += size


class _Box:
    __slots__ = ("type", "payload", "pos")

    def __init__(self, btype, payload, pos):
        self.type = btype
        self.payload = payload
        self.pos = pos


def _find(data, btype, start, end):
    for t, ps, pe, _ in _iter_boxes(data, start, end):
        if t == btype:
            return ps, pe
    return None


def _parse_children(data, start, end):
    out = []
    for t, ps, pe, _ in _iter_boxes(data, start, end):
        out.append(_Box(t, data[ps:pe], ps))
    return out


class Mp4File:
    def __init__(self, data):
        self.data = data
        self.tracks = []          # video tracks: dicts with sample tables
        self.duration_s = 0.0
        self._parse()

    def _parse(self):
        data = self.data
        moov = None
        for t, ps, pe, _ in _iter_boxes(data, 0, len(data)):
            if t == b"moov":
                moov = (ps, pe)
                break
        if moov is None:
            raise Mp4Error("no moov box (fragmented files not supported)")
        mps, mpe = moov
        mvhd = _find(data, b"mvhd", mps, mpe)
        timescale = 1000
        if mvhd:
            v = data[mvhd[0]]
            if v == 1:
                timescale = _u32(data, mvhd[0] + 20)
            else:
                timescale = _u32(data, mvhd[0] + 12)
        for t, ps, pe, _ in _iter_boxes(data, mps, mpe):
            if t != b"trak":
                continue
            tr = self._parse_trak(ps, pe, timescale)
            if tr is not None and tr["codec"] not in (b"mp4a", b"soun"):
                self.tracks.append(tr)
        if self.tracks:
            self.duration_s = max(tr["duration_s"] for tr in self.tracks)

    def _parse_trak(self, start, end, movie_timescale):
        data = self.data
        tkhd = _find(data, b"tkhd", start, end)
        width = height = 0.0
        if tkhd:
            v = data[tkhd[0]]
            # width/height are 16.16 fixed after the 76-byte (v0) /
            # 88-byte (v1) fixed header
            woff = 88 if v == 1 else 76
            width = _s16_16(data, tkhd[0] + woff)
            height = _s16_16(data, tkhd[0] + woff + 4)
        mdia = _find(data, b"mdia", start, end)
        if mdia is None:
            return None
        mps, mpe = mdia
        mdhd = _find(data, b"mdhd", mps, mpe)
        timescale = movie_timescale
        duration = 0
        if mdhd:
            v = data[mdhd[0]]
            if v == 1:
                timescale = _u32(data, mdhd[0] + 20)
                duration = _u64(data, mdhd[0] + 24)
            else:
                timescale = _u32(data, mdhd[0] + 12)
                duration = _u32(data, mdhd[0] + 16)
        hdlr = _find(data, b"hdlr", mps, mpe)
        handler = b""
        if hdlr:
            handler = data[hdlr[0] + 8:hdlr[0] + 12]
        minf = _find(data, b"minf", mps, mpe)
        if minf is None:
            return None
        stbl = _find(data, b"stbl", minf[0], minf[1])
        if stbl is None:
            return None
        sps, spe = stbl
        stsd = _find(data, b"stsd", sps, spe)
        codec = b""
        sps_list = []
        pps_list = []
        nal_len = 4
        if stsd:
            # stsd: version/flags u32, entry_count u32, then entry boxes;
            # each entry starts with its own 8-byte box header (size + fourcc)
            codec = data[stsd[0] + 12:stsd[0] + 16]
            if codec == b"avc1":
                # avcC lives after the 78-byte VisualSampleEntry inside the
                # entry box:  stsd payload starts at stsd[0] (version/flags +
                # entry_count = 8), the entry box header is 8 more, then the
                # 78-byte VisualSampleEntry, then avcC.
                avcc_off = stsd[0] + 16 + 78
                if avcc_off + 8 <= spe and data[avcc_off + 4:avcc_off + 8] == b"avcC":
                    p = avcc_off + 8
                    if p + 6 <= spe:
                        nal_len = (data[p + 4] & 0x03) + 1   # lengthSizeMinusOne+1
                        n_sps = data[p + 5] & 0x1F
                        p += 6
                        for _ in range(n_sps):
                            if p + 2 > spe:
                                break
                            ln = (data[p] << 8) | data[p + 1]   # 2-byte BE
                            p += 2
                            sps_list.append(data[p:p + ln])
                            p += ln
                        if p < spe:
                            n_pps = data[p]
                            p += 1
                            for _ in range(n_pps):
                                if p + 2 > spe:
                                    break
                                ln = (data[p] << 8) | data[p + 1]   # 2-byte BE
                                p += 2
                                pps_list.append(data[p:p + ln])
                                p += ln
        stts = _find(data, b"stts", sps, spe)
        deltas = []
        if stts:
            n = _u32(data, stts[0] + 4)   # entry_count after version/flags
            p = stts[0] + 8
            for _ in range(n):
                cnt = _u32(data, p)
                delta = _u32(data, p + 4)
                deltas.append((cnt, delta))
                p += 8
        stsz = _find(data, b"stsz", sps, spe)
        sizes = []
        if stsz:
            # layout: version/flags(4), sample_size(4), sample_count(4), sizes
            n = _u32(data, stsz[0] + 8)
            p = stsz[0] + 12
            for _ in range(n):
                sizes.append(_u32(data, p))
                p += 4
        stsc = _find(data, b"stsc", sps, spe)
        stco = _find(data, b"stco", sps, spe)
        co64 = _find(data, b"co64", sps, spe)
        offsets = []
        chunk_off_box = co64 if co64 else stco
        if chunk_off_box:
            n = _u32(data, chunk_off_box[0] + 4)
            p = chunk_off_box[0] + 8
            width4 = 8 if co64 else 4
            for _ in range(n):
                if co64:
                    offsets.append(_u64(data, p))
                else:
                    offsets.append(_u32(data, p))
                p += width4
        # build the per-sample offset list (stsc: samples-per-chunk runs)
        sample_offsets = []
        sc_runs = []
        if stsc:
            n = _u32(data, stsc[0] + 4)
            p = stsc[0] + 8
            for _ in range(n):
                first = _u32(data, p)
                cnt = _u32(data, p + 4)
                sc_runs.append((first, cnt))
                p += 12
        # expand stsz against chunks
        if stsc and offsets:
            si = 0
            run = 0
            for ci in range(len(offsets)):
                if run + 1 < len(sc_runs) and ci + 1 >= sc_runs[run + 1][0]:
                    run += 1
                spc = sc_runs[run][1]
                pos = offsets[ci]
                for _ in range(spc):
                    if si >= len(sizes):
                        break
                    sample_offsets.append(pos)
                    pos += sizes[si]
                    si += 1
        elif stsz and not stsc:
            # single chunk of all samples (very simple files)
            pos = offsets[0] if offsets else 0
            for s in sizes:
                sample_offsets.append(pos)
                pos += s
        # durations per sample
        durations = []
        for cnt, delta in deltas:
            durations.extend([delta] * cnt)
        if not durations:
            durations = [1] * len(sizes)
        # frame index
        frames = []
        for i in range(len(sizes)):
            frames.append({
                "offset": sample_offsets[i] if i < len(sample_offsets) else 0,
                "size": sizes[i],
                "duration": durations[i] if i < len(durations) else 1,
            })
        if not frames:
            return None
        total_ticks = sum(f["duration"] for f in frames)
        return {
            "codec": codec,
            "handler": handler,
            "width": width,
            "height": height,
            "timescale": timescale,
            "duration_s": total_ticks / float(timescale) if timescale else 0.0,
            "fps": timescale / float(frames[0]["duration"]) if frames[0]["duration"] else 0.0,
            "frames": frames,
            # H.264 (avc1): SPS/PPS from avcC + the sample NAL length field
            # size, so a player can build Annex-B streams for a decoder.
            "sps": sps_list,
            "pps": pps_list,
            "nal_len": nal_len,
        }

    def video_track(self):
        for tr in self.tracks:
            if tr["handler"] == b"vide" or tr["codec"] in (b"jpeg", b"raw ",
                                                           b"avc1", b"mp4v"):
                return tr
        return self.tracks[0] if self.tracks else None

    def frames(self, track=None):
        tr = track if track is not None else self.video_track()
        if tr is None:
            return []
        return tr["frames"]

    def read_frame(self, index, track=None):
        tr = track if track is not None else self.video_track()
        if tr is None:
            raise Mp4Error("no video track")
        if index < 0 or index >= len(tr["frames"]):
            raise Mp4Error("frame index out of range")
        f = tr["frames"][index]
        return self.data[f["offset"]:f["offset"] + f["size"]]
