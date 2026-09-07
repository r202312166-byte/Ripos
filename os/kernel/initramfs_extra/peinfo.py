# peinfo.py -- pure-Python PE/COFF parser for Ripos (M10.1).
#
# Parses Windows executables (PE32 / PE32+): DOS + NT headers, section
# table, import table (ILT/IAT), export table (EAT), base relocations and
# TLS/load-config.  Shared by both M10 backends (the native Wine-style
# loader and the pure-Python x86 interpreter) -- no C code, no ctypes.
#
# API:  PeFile(data)  ->  .is_pe32  .machine  .image_base  .entry_point
#        .sections[]  .imports[]  .exports[]  .relocations[]
#        .image_size  .subsystem  .characteristics

import struct


class PeError(Exception):
    pass


# image file machine types
MACHINE_I386 = 0x14C
MACHINE_AMD64 = 0x8664
MACHINE_NAMES = {MACHINE_I386: "i386", MACHINE_AMD64: "x86-64"}

# section characteristics
SEC_CNT_CODE = 0x00000020
SEC_CNT_INITIALIZED_DATA = 0x00000040
SEC_MEM_EXECUTE = 0x20000000
SEC_MEM_READ = 0x40000000
SEC_MEM_WRITE = 0x80000000

# directory entries
DIR_EXPORT = 0
DIR_IMPORT = 1
DIR_BASERELOC = 5
DIR_TLS = 9
DIR_LOAD_CONFIG = 10


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


class Section:
    __slots__ = ("name", "vaddr", "vsize", "raw_ptr", "raw_size", "chars")

    def __init__(self, name, vaddr, vsize, raw_ptr, raw_size, chars):
        self.name = name
        self.vaddr = vaddr
        self.vsize = vsize
        self.raw_ptr = raw_ptr
        self.raw_size = raw_size
        self.chars = chars

    def is_code(self):
        return bool(self.chars & (SEC_CNT_CODE | SEC_MEM_EXECUTE))


class PeFile:
    def __init__(self, data):
        self.data = data
        self.is_pe32 = True
        self.machine = 0
        self.sections = []
        self.imports = []
        self.exports = []       # (name, ordinal, rva)
        self.relocations = []   # (rva, count, type)
        self.image_base = 0
        self.entry_point = 0
        self.image_size = 0
        self.subsystem = 0
        self.characteristics = 0
        self.section_align = 0
        self.file_align = 0
        self._dirs = [0] * 16
        self._parse(data)

    def _rva_to_off(self, rva):
        for s in self.sections:
            if s.vaddr <= rva < s.vaddr + max(s.vsize, s.raw_size):
                if rva - s.vaddr < s.raw_size:
                    return s.raw_ptr + (rva - s.vaddr)
                return None
        return None

    def _read_cstr(self, off, maxlen=4096):
        end = self.data.find(b"\x00", off, min(len(self.data), off + maxlen))
        if end < 0:
            return ""
        return self.data[off:end].decode("ascii", "replace")

    def _parse(self, data):
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise PeError("not a PE file (missing MZ header)")
        e_lfanew = _u32(data, 0x3C)
        if e_lfanew + 4 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            raise PeError("no PE signature at e_lfanew")
        p = e_lfanew + 4
        self.machine = _u16(data, p)
        self.characteristics = _u16(data, p + 18)
        num_sections = _u16(data, p + 2)
        opt_size = _u16(data, p + 16)
        # optional header starts right after the 20-byte COFF header
        op = p + 20
        opt_magic = _u16(data, op)
        if opt_magic == 0x10B:
            self.is_pe32 = True
        elif opt_magic == 0x20B:
            self.is_pe32 = False
        else:
            raise PeError("unknown optional header magic %#x" % opt_magic)
        # optional header fields (offsets differ for PE32/PE32+)
        if self.is_pe32:
            self.entry_point = _u32(data, op + 16)
            self.image_base = _u32(data, op + 28)
            self.section_align = _u32(data, op + 32)
            self.file_align = _u32(data, op + 36)
            self.image_size = _u32(data, op + 56)
            self.subsystem = _u16(data, op + 68)
            dir_off = op + 96
        else:
            self.entry_point = _u32(data, op + 16)
            self.image_base = _u64(data, op + 24)
            self.section_align = _u32(data, op + 32)
            self.file_align = _u32(data, op + 36)
            self.image_size = _u32(data, op + 56)
            self.subsystem = _u16(data, op + 68)
            dir_off = op + 112
        for i in range(16):
            self._dirs[i] = (_u32(data, dir_off + i * 8), _u32(data, dir_off + i * 8 + 4))
        # sections: right after the COFF header (20) + optional header
        sp = p + 20 + opt_size
        for i in range(num_sections):
            o = sp + i * 40
            name = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
            vsize = _u32(data, o + 8)
            vaddr = _u32(data, o + 12)
            raw_size = _u32(data, o + 16)
            raw_ptr = _u32(data, o + 20)
            chars = _u32(data, o + 36)
            self.sections.append(Section(name, vaddr, vsize, raw_ptr, raw_size, chars))
        self._parse_imports()
        self._parse_exports()
        self._parse_relocations()

    def _parse_imports(self):
        rva, size = self._dirs[DIR_IMPORT]
        if not rva:
            return
        off = self._rva_to_off(rva)
        if off is None:
            return
        w = 8 if self.is_pe32 else 16
        i = 0
        while True:
            o = off + i * (20 if self.is_pe32 else 24)
            if o + 20 > len(self.data):
                break
            offt = _u32(self.data, o)       # OriginalFirstThunk (ILT)
            name_rva = _u32(self.data, o + 12)
            first = _u32(self.data, o + 16)  # FirstThunk (IAT)
            if offt == 0 and name_rva == 0 and first == 0:
                break
            if name_rva == 0:
                i += 1
                continue
            name_off = self._rva_to_off(name_rva)
            if name_off is None:
                i += 1
                continue
            dll = self._read_cstr(name_off)
            funcs = []
            iat_slots = []   # RVAs of the IAT slots (FirstThunk-based)
            thunk_rva = offt if offt else first
            thunk_off = self._rva_to_off(thunk_rva)
            if thunk_off is not None:
                j = 0
                while True:
                    if self.is_pe32:
                        val = _u32(self.data, thunk_off + j * 4)
                    else:
                        val = _u64(self.data, thunk_off + j * 8)
                    if val == 0:
                        break
                    if val & (0x80000000 if self.is_pe32 else 0x8000000000000000):
                        funcs.append("ordinal:%d" % (val & 0xFFFF))
                    else:
                        hint_off = self._rva_to_off(val & 0x7FFFFFFF)
                        if hint_off is not None:
                            funcs.append(self._read_cstr(hint_off + 2))
                        else:
                            funcs.append("rva:%x" % (val & 0x7FFFFFFF))
                    # the matching IAT slot (FirstThunk + j*word)
                    iat_slots.append((first + j * (4 if self.is_pe32 else 8)) & 0xFFFFFFFF)
                    j += 1
            self.imports.append((dll, funcs, iat_slots))
            i += 1

    def _parse_exports(self):
        rva, size = self._dirs[DIR_EXPORT]
        if not rva:
            return
        off = self._rva_to_off(rva)
        if off is None:
            return
        num_functions = _u32(self.data, off + 20)
        num_names = _u32(self.data, off + 24)
        addr_rva = _u32(self.data, off + 28)
        name_rva = _u32(self.data, off + 32)
        ordinal_rva = _u32(self.data, off + 36)
        addr_off = self._rva_to_off(addr_rva)
        name_off = self._rva_to_off(name_rva)
        ord_off = self._rva_to_off(ordinal_rva)
        base = _u32(self.data, off + 16)
        if addr_off is None:
            return
        addrs = [_u32(self.data, addr_off + 4 * i) for i in range(num_functions)]
        names = []
        ordinals = []
        if name_off is not None:
            for i in range(num_names):
                nr = _u32(self.data, name_off + 4 * i)
                no = self._rva_to_off(nr)
                names.append(self._read_cstr(no) if no is not None else "")
        if ord_off is not None:
            for i in range(num_names):
                ordinals.append(_u16(self.data, ord_off + 2 * i))
        for i in range(num_names):
            if i < len(ordinals) and ordinals[i] < len(addrs):
                self.exports.append((names[i], base + ordinals[i], addrs[ordinals[i]]))
        # unnamed exports
        for i, a in enumerate(addrs):
            if a and not any(a == e[2] for e in self.exports):
                self.exports.append(("", base + i, a))

    def _parse_relocations(self):
        rva, size = self._dirs[DIR_BASERELOC]
        if not rva:
            return
        off = self._rva_to_off(rva)
        if off is None:
            return
        end = off + size
        while off + 8 <= end and off + 8 <= len(self.data):
            page = _u32(self.data, off)
            block_size = _u32(self.data, off + 4)
            if block_size < 8:
                break
            n = (block_size - 8) // 2
            entries = []
            for j in range(n):
                e = _u16(self.data, off + 8 + 2 * j)
                entries.append((page + (e & 0xFFF), e >> 12))
            self.relocations.append((page, n, entries))
            off += block_size