# peexec-dll-test.py -- M10 Method-B DLL loading: a PE32 loads a PE32 DLL,
# resolves the exe's imports to the DLL's exports, and the interpreter runs
# the DLL's own code (my_add: mov eax,[esp+4]; add eax,[esp+8]; ret 8).
# The DLL is hand-assembled i386 (mingw's -m32 multilib is unavailable here;
# the mechanism is identical for a mingw-built i386 DLL).
import os
import pathlib
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import peexec   # noqa: E402
import peinfo   # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("peexec-dll-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("peexec-dll-test FAIL %s%s" % (name, extra))


def u16(v):
    return struct.pack("<H", v)


def u32(v):
    return struct.pack("<I", v)


def pe32(image_base, nsec, sections, dirs, entry_rva):
    """Build a minimal PE32.  sections: list of (name, vsize, vaddr,
    raw_ptr, raw_size, chars, bytes).  dirs: {index: (rva, size)}."""
    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    pe = bytearray()
    pe += b"PE\x00\x00"
    pe += u16(0x14C) + u16(nsec) + u32(0) + u32(0) + u32(0)
    pe += u16(0xE0) + u16(0x010F)
    opt = bytearray(0xE0)
    struct.pack_into("<H", opt, 0, 0x10B)
    struct.pack_into("<I", opt, 16, entry_rva)
    struct.pack_into("<I", opt, 28, image_base)
    struct.pack_into("<I", opt, 32, 0x1000)
    struct.pack_into("<I", opt, 36, 0x200)
    struct.pack_into("<I", opt, 56, (nsec + 1) * 0x1000)
    struct.pack_into("<I", opt, 60, 0x200)
    struct.pack_into("<H", opt, 68, 3)
    struct.pack_into("<I", opt, 92, 16)
    for idx, (rva, size) in dirs.items():
        struct.pack_into("<II", opt, 96 + idx * 8, rva, size)
    secs = bytearray()
    for (name, vsize, vaddr, raw_ptr, raw_size, chars, _b) in sections:
        h = bytearray(40)
        h[0:len(name)] = name
        struct.pack_into("<IIIIII", h, 8, vsize, vaddr, raw_size, raw_ptr, 0, 0)
        struct.pack_into("<I", h, 36, chars)
        secs += h
    file_img = bytes(dos) + bytes(pe) + bytes(opt) + bytes(secs)
    file_img += b"\x00" * (0x200 - len(file_img))
    for (sname, _v, _a, _rp, rs, _c, data) in sections:
        assert len(data) <= rs, ("section %r data %d > raw size %d" % (sname, len(data), rs))
        file_img += bytes(data) + b"\x00" * (rs - len(data))
    return file_img


# ============ build the DLL ============
DLL_IMG = 0x10000000
my_add_code = bytes.fromhex("8B442404") + bytes.fromhex("03442408") + bytes.fromhex("C20800")  # mov eax,[esp+4]; add eax,[esp+8]; ret 8
greet_rva = 0x2000
my_greet_code = b"\xB8" + u32(DLL_IMG + greet_rva) + b"\xC3"          # mov eax, greeting; ret
text = my_add_code + b"\x00" * (0x10 - len(my_add_code)) + my_greet_code  # my_add @0x1000, my_greet @0x1010
EAT = 0x2010
FUNCS, NAMES, ORDS = 0x2040, 0x2048, 0x2050
STR1, STR2, STR3 = 0x2058, 0x2064, 0x2070
rdata = bytearray(0x200)
rdata[0:0 + len(b"greetings\x00")] = b"greetings\x00"
struct.pack_into("<IIHHIIIIIII", rdata, EAT - 0x2000,
                  0, 0, 0, 0, STR3, 1, 2, 2, FUNCS, NAMES, ORDS)
struct.pack_into("<II", rdata, FUNCS - 0x2000, 0x1000, 0x1010)
struct.pack_into("<II", rdata, NAMES - 0x2000, STR1, STR2)
struct.pack_into("<HH", rdata, ORDS - 0x2000, 0, 1)
rdata[STR1 - 0x2000:STR1 - 0x2000 + 7] = b"my_add\x00"
rdata[STR2 - 0x2000:STR2 - 0x2000 + 9] = b"my_greet\x00"
rdata[STR3 - 0x2000:STR3 - 0x2000 + 10] = b"mydll.dll\x00"
dll_img = pe32(DLL_IMG, 2, [
    (b".text", 0x200, 0x1000, 0x200, 0x200, 0x60000020, text),
    (b".rdata", 0x200, 0x2000, 0x400, 0x200, 0x40000040, bytes(rdata)),
], {0: (EAT, 0x70)}, 0)

# ============ build the exe ============
EXE_IMG = 0x400000
MSG = b"Hello, Ripos!\r\n"
NBYTES = len(MSG)
RVA_TEXT, RVA_RDATA, RVA_DATA, RVA_IDATA = 0x1000, 0x2000, 0x3000, 0x4000
desc1_ilt, desc1_iat = 0x403C, 0x4044
desc2_ilt, desc2_iat = 0x404C, 0x4058
ID = bytearray()
ID += b"\x00" * 60                 # 3 descriptors (kernel32, mydll, null)
ID += b"\x00" * 8                  # ILT1
ID += b"\x00" * 8                  # IAT1
ID += b"\x00" * 12                 # ILT2
ID += b"\x00" * 12                 # IAT2


def add_name(s):
    global ID
    off = len(ID)
    ID += u16(0) + s + b"\x00"
    return RVA_IDATA + off


hn_exit = add_name(b"ExitProcess")
hn_add = add_name(b"my_add")
hn_greet = add_name(b"my_greet")
name_k32 = RVA_IDATA + len(ID)
ID += b"kernel32.dll\x00"
name_mydll = RVA_IDATA + len(ID)
ID += b"mydll.dll\x00"
# patch ILTs and IATs into their placeholder slots
struct.pack_into("<II", ID, desc1_ilt - RVA_IDATA, hn_exit, 0)
struct.pack_into("<II", ID, desc1_iat - RVA_IDATA, hn_exit, 0)
struct.pack_into("<III", ID, desc2_ilt - RVA_IDATA, hn_add, hn_greet, 0)
struct.pack_into("<III", ID, desc2_iat - RVA_IDATA, hn_add, hn_greet, 0)
# descriptors
struct.pack_into("<IIIII", ID, 0, desc1_ilt, 0, 0, name_k32, desc1_iat)
struct.pack_into("<IIIII", ID, 20, desc2_ilt, 0, 0, name_mydll, desc2_iat)
# exe code: my_add(3,4) then ExitProcess(result)
code = bytes.fromhex("6A04")                      # push 4
code += bytes.fromhex("6A03")                     # push 3
code += b"\xFF\x15" + u32(EXE_IMG + desc2_iat)    # call [mydll.my_add]   -> eax=7
code += bytes.fromhex("50")                       # push eax
code += b"\xFF\x15" + u32(EXE_IMG + desc1_iat)    # call [ExitProcess]
text_e = bytes(code) + b"\x00" * (0x200 - len(code))
rdata_e = MSG + b"\x00" * (0x200 - len(MSG))
data_e = u32(0) + b"\x00" * (0x200 - 4)
exe_img = pe32(EXE_IMG, 4, [
    (b".text", 0x200, RVA_TEXT, 0x200, 0x200, 0x60000020, text_e),
    (b".rdata", 0x200, RVA_RDATA, 0x400, 0x200, 0x40000040, rdata_e),
    (b".data", 0x200, RVA_DATA, 0x600, 0x200, 0xC0000040, data_e),
    (b".idata", 0x200, RVA_IDATA, 0x800, 0x200, 0xC0000040, bytes(ID)),
], {1: (RVA_IDATA, 60)}, RVA_TEXT)

# ============ run ============
r = peexec.PeRunner()
dll_exports = r.load_dll(dll_img, "mydll.dll")
check("dll exports", set(dll_exports) == {"my_add", "my_greet"},
      " %r" % (sorted(dll_exports),))
code_ret = r.run(exe_img)
check("exit code = my_add(3,4)", code_ret == 7, " got %d" % code_ret)
# my_greet returns a POINTER to the string in eax; verify by calling it
m = r.m
m.regs["esp"] = len(m.mem) - 16
m.push32(m.end_marker)
m.regs["eip"] = dll_exports["my_greet"]
m.run(m.regs["eip"], steps=1000)
s = bytes(m.mem[m.regs["eax"]:m.regs["eax"] + 12]).split(b"\x00")[0]
check("my_greet string", s == b"greetings", " got %r" % (s,))
# the DLL code really ran in the interpreter: my_add's bytes are mapped
add = dll_exports["my_add"]
check("my_add code mapped", bytes(r.m.mem[add:add + 4]) == bytes.fromhex("8B442404"),
      " %r" % (bytes(r.m.mem[add:add + 4]),))

print("peexec-dll-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)