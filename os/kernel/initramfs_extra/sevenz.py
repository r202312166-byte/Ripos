# sevenz.py -- minimal pure-Python 7z container codec for Ripos (M9.3).
#
# Parses the 7z archive container (signature, next header, PackInfo /
# UnpackInfo / SubStreamsInfo, FilesInfo) and decompresses folders with the
# stdlib lzma module (LZMA1 / LZMA2 raw streams) -- no C deps beyond _lzma
# (M9.1).  Supports solid and non-solid archives, LZMA1 and LZMA2 coders,
# empty files, encoded (compressed) headers and UTF-16LE names.
# BCJ (branch) filter chains and encryption are rejected with a clear error.
#
# API:  SevenZipFile(data)  ->  .namelist()  .read(name)  .extract_to(dir)
#
# The container layout follows the public 7z format description; the parsing
# mirrors py7zr's (reference implementation), comments kept for audit.

import lzma
import os
import struct


class SevenZipError(Exception):
    pass


K_END = 0x00
K_HEADER = 0x01
K_MAIN_STREAMS_INFO = 0x04
K_FILES_INFO = 0x05
K_PACK_INFO = 0x06
K_UNPACK_INFO = 0x07
K_SUBSTREAMS_INFO = 0x08
K_SIZE = 0x09
K_CRC = 0x0A
K_FOLDER = 0x0B
K_CODERS_UNPACK_SIZE = 0x0C
K_NUM_UNPACK_STREAM = 0x0D
K_EMPTY_STREAM = 0x0E
K_EMPTY_FILE = 0x0F
K_NAME = 0x11
K_ATTRIBUTES = 0x15
K_ENCODED_HEADER = 0x17
K_DUMMY = 0x19

CODER_LZMA = b"\x03\x01\x01"
CODER_LZMA2 = b"\x21"
BRANCH_IDS = (b"\x03", b"\x04", b"\x05", b"\x07", b"\x08", b"\x09",
              b"\x03\x03\x01\x03", b"\x03\x03\x02\x05",
              b"\x03\x03\x05\x01", b"\x03\x03\x07\x01",
              b"\x03\x03\x08\x05")

_SIG = b"7z\xbc\xaf\x27\x1c"
AFTER_HEADER = 32  # signature(6) + version(2) + crc(4) + 8 + 8 + 4


_X86_ALLOWED = (0, 1, 2, 4, 8, 9, 10, 12)
_X86_BITNUM = (0, 1, 2, 2, 3, 3, 3, 3)


def _x86_decode(data):
    """BCJ X86 branch converter, decode direction.  Faithful port of
    liblzma/simple/x86.c (as used by the py7zr bcj reference), single-shot
    over the whole stream."""
    size = len(data)
    if size < 5:
        return bytes(data)
    buf = bytearray(data)
    prev_mask = 0
    prev_pos = -5
    current_pos = 0
    limit = size - 5
    buffer_pos = 0
    pos1 = 0
    pos2 = 0
    while buffer_pos <= limit:
        if pos1 >= 0:
            pos1 = buf.find(0xE9, buffer_pos, limit)
        if pos2 >= 0:
            pos2 = buf.find(0xE8, buffer_pos, limit)
        if pos1 < 0 and pos2 < 0:
            buffer_pos = limit + 1
            break
        elif pos1 < 0:
            buffer_pos = pos2
        elif pos2 < 0:
            buffer_pos = pos1
        else:
            buffer_pos = min(pos1, pos2)
        offset = current_pos + buffer_pos - prev_pos
        prev_pos = current_pos + buffer_pos
        if offset > 5:
            prev_mask = 0
        else:
            for _ in range(offset):
                prev_mask &= 0x77
                prev_mask <<= 1
        if (buf[buffer_pos + 4] in (0, 0xFF)
                and (prev_mask >> 1) in _X86_ALLOWED):
            src = struct.unpack("<L", buf[buffer_pos + 1:buffer_pos + 5])[0]
            distance = current_pos + buffer_pos + 5
            idx = _X86_BITNUM[prev_mask >> 1]
            while True:
                dest = (src - distance) & 0xFFFFFFFF
                if prev_mask == 0:
                    break
                b = 0xFF & (dest >> (24 - idx * 8))
                if not (b == 0 or b == 0xFF):
                    break
                src = dest ^ ((1 << (32 - idx * 8)) - 1) & 0xFFFFFFFF
            buf[buffer_pos + 1:buffer_pos + 4] = (dest & 0xFFFFFF).to_bytes(3, "little")
            buf[buffer_pos + 4] = 0x00 if ((dest >> 24) & 1) == 0 else 0xFF
            buffer_pos += 5
            prev_mask = 0
        else:
            buffer_pos += 1
            prev_mask |= 1
            if buffer_pos + 3 < size and buf[buffer_pos + 3] in (0, 0xFF):
                prev_mask |= 0x10
    return bytes(buf)


# ---------------------------------------------------------------------------
# low-level readers
# ---------------------------------------------------------------------------

def _varint(data, pos):
    """7z UINT64: the first byte encodes the extra-byte count and the
    high bits of the value (py7zr write_uint64 / read_uint64 scheme)."""
    b = data[pos]
    pos += 1
    if b == 0xFF:
        return struct.unpack("<Q", data[pos:pos + 8])[0], pos + 8
    blen = [(0b01111111, 0), (0b10111111, 1), (0b11011111, 2),
            (0b11101111, 3), (0b11110111, 4), (0b11111011, 5),
            (0b11111101, 6), (0b11111110, 7)]
    mask = 0x80
    vlen = 8
    for v, l in blen:
        if b <= v:
            vlen = l
            break
        mask >>= 1
    if vlen == 0:
        return b & (mask - 1), pos
    val = int.from_bytes(data[pos:pos + vlen], "little")
    pos += vlen
    return val + ((b & (mask - 1)) << (vlen * 8)), pos


def _read_bools(data, pos, count):
    bits = []
    for i in range(count):
        bits.append((data[pos + i // 8] >> (7 - (i % 8))) & 1)
    return bits, pos + (count + 7) // 8


def _read_checkall_bools(data, pos, count):
    """Boolean vector with py7zr's 'checkall' optimization: a leading 0x01
    means every item is True (no bitmap follows); 0x00 means a bitmap."""
    all_def = data[pos]
    pos += 1
    if all_def != 0:
        return [True] * count, pos
    return _read_bools(data, pos, count)


def _read_data(data, pos):
    size, pos = _varint(data, pos)
    return data[pos:pos + size], pos + size


def _utf16_names(body):
    """Parse the NAME property body (after the external byte): UTF-16LE
    strings, each terminated by a null word."""
    text = body.decode("utf-16le")
    return [n.replace("\\", "/") for n in text.split("\x00") if n]


# ---------------------------------------------------------------------------
# coder / folder parsing
# ---------------------------------------------------------------------------

class Folder:
    __slots__ = ("coders", "bind_pairs", "packed_indices", "unpack_sizes",
                 "digestdefined")

    def __init__(self):
        self.coders = []          # list of (coder_id, num_in, num_out, props)
        self.bind_pairs = []      # (in_coder_idx, out_coder_idx)
        self.packed_indices = []
        self.unpack_sizes = []    # per coder output
        self.digestdefined = False

    def get_unpack_size(self):
        if not self.unpack_sizes:
            return 0
        for i in range(len(self.unpack_sizes) - 1, -1, -1):
            if not any(o == i for (_, o) in self.bind_pairs):
                return self.unpack_sizes[i]
        return self.unpack_sizes[-1]


def _parse_folder(data, pos):
    f = Folder()
    num_coders, pos = _varint(data, pos)
    total_in = 0
    total_out = 0
    for _ in range(num_coders):
        flags = data[pos]
        pos += 1
        id_size = flags & 0x0F
        is_complex = bool(flags & 0x10)
        has_attrs = bool(flags & 0x20)
        cid = data[pos:pos + id_size]
        pos += id_size
        num_in = num_out = 1
        if is_complex:
            num_in, pos = _varint(data, pos)
            num_out, pos = _varint(data, pos)
        props = b""
        if has_attrs:
            props, pos = _read_data(data, pos)
        f.coders.append((cid, num_in, num_out, props))
        total_in += num_in
        total_out += num_out
    num_bind = total_out - 1
    for _ in range(num_bind):
        i, pos = _varint(data, pos)
        o, pos = _varint(data, pos)
        f.bind_pairs.append((i, o))
    num_packed = total_in - num_bind
    if num_packed == 1:
        # the single input coder that is not bound
        bound_ins = set(i for (i, _) in f.bind_pairs)
        f.packed_indices = [i for i in range(num_coders) if i not in bound_ins]
    else:
        for _ in range(num_packed):
            pi, pos = _varint(data, pos)
            f.packed_indices.append(pi)
    return f, pos


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

class SevenZipFile:
    def __init__(self, data):
        self.data = data
        self.files = []       # {name, size, folder, sub}
        self._folders = []
        self._pack_pos = 0
        self._pack_sizes = []
        self._sub_sizes = []  # per folder: [sizes...]
        self._decode_cache = {}
        self._parse(data)

    # -- top level -------------------------------------------------------

    def _parse(self, data):
        if not data.startswith(_SIG):
            raise SevenZipError("not a 7z archive")
        pos = 8 + 4  # sig + version + start-crc
        next_off = struct.unpack("<Q", data[pos:pos + 8])[0]
        next_size = struct.unpack("<Q", data[pos + 8:pos + 16])[0]
        header = data[AFTER_HEADER + next_off: AFTER_HEADER + next_off + next_size]
        if not header:
            return  # empty archive
        if header[0] == K_ENCODED_HEADER:
            header = self._decode_encoded_header(header)
        self._parse_header(header)

    def _decode_encoded_header(self, block):
        pos = 1  # skip K_ENCODED_HEADER
        pack_pos, pack_sizes, folders = self._parse_streams_block(block, pos)
        if len(folders) != 1 or not pack_sizes:
            raise SevenZipError("encoded header: expected one packed folder")
        packed = self._read_packed(pack_pos, pack_sizes[0])
        return self._decode_folder(folders[0], packed)

    def _parse_header(self, data):
        pos = 0
        while pos < len(data):
            pid = data[pos]
            pos += 1
            if pid == K_HEADER:
                pass  # marker only; the next property follows directly
            elif pid == K_MAIN_STREAMS_INFO:
                self._pack_pos, self._pack_sizes, self._folders, self._sub_sizes, pos =                     self._parse_main_streams(data, pos)
            elif pid == K_FILES_INFO:
                pos = self._parse_files_info(data, pos)
            elif pid == K_END:
                break
            else:
                raise SevenZipError("unknown header property %#x" % pid)

    # -- streams ---------------------------------------------------------

    def _parse_main_streams(self, data, pos):
        pack_pos = 0
        pack_sizes = []
        folders = []
        sub_sizes = []
        while True:
            pid = data[pos]
            pos += 1
            if pid == K_PACK_INFO:
                pack_pos, pos = _varint(data, pos)
                num_pack, pos = _varint(data, pos)
                while True:
                    p2 = data[pos]
                    pos += 1
                    if p2 == K_SIZE:
                        for _ in range(num_pack):
                            s, pos = _varint(data, pos)
                            pack_sizes.append(s)
                    elif p2 == K_CRC:
                        ext = data[pos]
                        pos += 1
                        if ext == 0:
                            pos += 4 * num_pack
                        else:
                            bits, pos = _read_bools(data, pos, num_pack)
                            pos += 4 * sum(bits)
                    elif p2 == K_END:
                        break
            elif pid == K_UNPACK_INFO:
                folders, pos = self._parse_unpack_info(data, pos)
            elif pid == K_SUBSTREAMS_INFO:
                sub_sizes, pos = self._parse_substreams(data, pos, folders)
            elif pid == K_END:
                break
            else:
                raise SevenZipError("unknown stream property %#x" % pid)
        if not sub_sizes:
            # no SubStreamsInfo: one substream per folder, sized by the folder
            sub_sizes = [[f.get_unpack_size()] for f in folders]
        return pack_pos, pack_sizes, folders, sub_sizes, pos

    def _parse_streams_block(self, data, pos):
        """packinfo + unpackinfo (used for encoded headers)."""
        pack_pos = 0
        pack_sizes = []
        folders = []
        while True:
            pid = data[pos]
            pos += 1
            if pid == K_PACK_INFO:
                pack_pos, pos = _varint(data, pos)
                num_pack, pos = _varint(data, pos)
                while True:
                    p2 = data[pos]
                    pos += 1
                    if p2 == K_SIZE:
                        for _ in range(num_pack):
                            s, pos = _varint(data, pos)
                            pack_sizes.append(s)
                    elif p2 == K_END:
                        break
            elif pid == K_UNPACK_INFO:
                folders, pos = self._parse_unpack_info(data, pos)
            elif pid == K_END:
                break
            else:
                raise SevenZipError("unknown encoded-header property %#x" % pid)
        return pack_pos, pack_sizes, folders

    def _parse_unpack_info(self, data, pos):
        folders = []
        while True:
            pid = data[pos]
            pos += 1
            if pid == K_FOLDER:
                num, pos = _varint(data, pos)
                ext = data[pos]
                pos += 1
                if ext != 0:
                    raise SevenZipError("external folders not supported")
                for _ in range(num):
                    f, pos = _parse_folder(data, pos)
                    folders.append(f)
            elif pid == K_CODERS_UNPACK_SIZE:
                for f in folders:
                    for (_, _, num_out, _) in f.coders:
                        for _ in range(num_out):
                            s, pos = _varint(data, pos)
                            f.unpack_sizes.append(s)
            elif pid == K_CRC:
                defined, pos = _read_checkall_bools(data, pos, len(folders))
                for i, d in enumerate(defined):
                    folders[i].digestdefined = bool(d)
                pos += 4 * len(folders)  # py7zr stores one crc per folder
            elif pid == K_END:
                break
            else:
                raise SevenZipError("unknown unpack-info property %#x" % pid)
        return folders, pos

    def _parse_substreams(self, data, pos, folders):
        num_folders = len(folders)
        num_per = [1] * num_folders
        pid = data[pos]
        if pid == K_NUM_UNPACK_STREAM:
            pos += 1
            num_per = []
            for _ in range(num_folders):
                v, pos = _varint(data, pos)
                num_per.append(v)
            pid = data[pos]
        sub_sizes = []
        if pid == K_SIZE:
            pos += 1
            for i in range(num_folders):
                total = 0
                for j in range(1, num_per[i]):
                    s, pos = _varint(data, pos)
                    sub_sizes.append(s)
                    total += s
                sub_sizes.append(folders[i].get_unpack_size() - total)
            pid = data[pos]
        if pid == K_CRC:
            pos += 1
            num_digests = 0
            for i in range(num_folders):
                if num_per[i] != 1 or not folders[i].digestdefined:
                    num_digests += num_per[i]
            _, pos = _read_checkall_bools(data, pos, num_digests)
            pos += 4 * num_digests  # py7zr stores a crc per digest
            pid = data[pos]
        if pid != K_END:
            raise SevenZipError("substreams without end property (pid %#x)" % pid)
        pos += 1
        # split flat sub_sizes into per-folder lists; folders without an
        # explicit size section hold one substream of the folder size
        out = []
        i = 0
        for n, f in zip(num_per, folders):
            if n == 1 and i >= len(sub_sizes):
                out.append([f.get_unpack_size()])
            else:
                out.append(sub_sizes[i:i + n])
                i += n
        return out, pos

    # -- files -----------------------------------------------------------

    def _parse_files_info(self, data, pos):
        num_files, pos = _varint(data, pos)
        files = [{"emptystream": False} for _ in range(num_files)]
        num_empty = 0
        while True:
            pid = data[pos]
            pos += 1
            if pid == K_END:
                break
            size, pos = _varint(data, pos)
            body = data[pos:pos + size]
            pos += size
            if pid == K_DUMMY:
                continue
            if pid == K_EMPTY_STREAM:
                bits, _ = _read_bools(body, 0, num_files)
                for i, b in enumerate(bits):
                    files[i]["emptystream"] = bool(b)
                    num_empty += b
            elif pid == K_EMPTY_FILE:
                pass  # (empty files have no data; size 0 is implicit)
            elif pid == K_NAME:
                if not body or body[0] != 0:
                    raise SevenZipError("external names not supported")
                names = _utf16_names(body[1:])
                for i, f in enumerate(files):
                    if i < len(names):
                        f["name"] = names[i]
            elif pid in (K_ATTRIBUTES,):
                pass  # not needed
            # creation/access/write times, start-pos: ignored
        # assign names to any file missing one
        for i, f in enumerate(files):
            if "name" not in f:
                f["name"] = ""
        # map non-empty files to (folder, substream) in order
        fi = 0
        folder_idx = 0
        sub_idx = 0
        for f in files:
            if f["emptystream"]:
                f["folder"] = -1
                f["sub"] = 0
                f["size"] = 0
                continue
            while folder_idx < len(self._folders) and sub_idx >= len(self._sub_sizes[folder_idx]):
                folder_idx += 1
                sub_idx = 0
            if folder_idx >= len(self._folders):
                f["folder"] = -1
                f["sub"] = 0
                f["size"] = 0
                continue
            f["folder"] = folder_idx
            f["sub"] = sub_idx
            f["size"] = self._sub_sizes[folder_idx][sub_idx]
            sub_idx += 1
            fi += 1
        self.files = files
        return pos

    # -- data ------------------------------------------------------------

    def _read_packed(self, pack_pos, size):
        start = AFTER_HEADER + pack_pos
        return self.data[start:start + size]

    def _decode_folder(self, folder, packed):
        coders = folder.coders
        if len(coders) > 1:
            # only simple sequential chains are supported (coder o's output
            # feeds coder i's input with i == o + 1)
            for (i, o) in folder.bind_pairs:
                if i != o + 1 or i >= len(coders):
                    raise SevenZipError("complex coder binding not supported")
        out = packed
        for c in coders:
            out = self._apply_coder(c, out)
        return out

    def _apply_coder(self, coder, data):
        cid, num_in, num_out, props = coder
        if num_in != 1 or num_out != 1:
            raise SevenZipError("complex coder not supported")
        if cid == CODER_LZMA2:
            return self._raw_decompress(data,
                                        [{"id": lzma.FILTER_LZMA2}])
        if cid == CODER_LZMA:
            if len(props) < 5:
                raise SevenZipError("bad LZMA1 properties")
            # 7z LZMA property byte: (pb * 5 + lp) * 9 + lc
            lc = props[0] % 9
            lp = (props[0] // 9) % 5
            pb = props[0] // 45
            dict_size = struct.unpack("<I", props[1:5])[0]
            return self._raw_decompress(data, [{"id": lzma.FILTER_LZMA1,
                                                "lc": lc, "lp": lp, "pb": pb,
                                                "dict_size": dict_size}])
        if cid in (b"\x03\x03\x01\x03",):  # BCJ X86
            return _x86_decode(data)
        if cid in BRANCH_IDS:
            raise SevenZipError("BCJ branch coder %r not supported" % cid)
        raise SevenZipError("unknown 7z coder %r" % cid)

    @staticmethod
    def _raw_decompress(packed, filters):
        """Raw LZMA1/LZMA2 decompress (single call, no max_length)."""
        d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        try:
            return d.decompress(packed)
        except lzma.LZMAError:
            # raw LZMA1 stream without an end marker
            return b""

    # -- public API ------------------------------------------------------

    def namelist(self):
        return [f["name"] for f in self.files if f.get("folder", -1) >= 0
                or not f.get("emptystream", False)]

    def read(self, name):
        for f in self.files:
            if f.get("name") == name:
                if f["folder"] < 0:
                    return b""
                out = self._decode_folder_packed(f["folder"])
                size = f["size"]
                offset = sum(self._sub_sizes[f["folder"]][:f["sub"]])
                return out[offset:offset + size]
        raise SevenZipError("no such file: %s" % name)

    def _decode_folder_packed(self, idx):
        """Decode folder idx (cached by index)."""
        if idx in self._decode_cache:
            return self._decode_cache[idx]
        folder = self._folders[idx]
        start = AFTER_HEADER + self._pack_pos
        packed = b""
        for pi in folder.packed_indices:
            if pi < len(self._pack_sizes):
                packed += self.data[start:start + self._pack_sizes[pi]]
                start += self._pack_sizes[pi]
        out = self._decode_folder(folder, packed)
        self._decode_cache[idx] = out
        return out

    def extract_to(self, outdir):
        for f in self.files:
            target = os.path.join(outdir, f["name"])
            if f["name"].endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            d = os.path.dirname(target)
            if d:
                os.makedirs(d, exist_ok=True)
            if f["folder"] < 0:
                with open(target, "wb"):
                    pass  # empty file
            else:
                with open(target, "wb") as fh:
                    fh.write(self.read(f["name"]))
