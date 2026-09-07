# peexec-test.py -- M10 Method-B gate: a real PE32 runs in the pure-Python
# x86 interpreter with Win32 shims (GetStdHandle/WriteFile/ExitProcess).
# The PE is hand-assembled (DOS+NT headers, .text/.rdata/.data/.idata with a
# 3-import kernel32 table), exactly like a -nostdlib hello.
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
        print("peexec-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("peexec-test FAIL %s%s" % (name, extra))


def u16(v):
    return struct.pack("<H", v)


def u32(v):
    return struct.pack("<I", v)


IMG = 0x400000
SEC_ALIGN = 0x1000
FILE_ALIGN = 0x200
MSG = b"Hello, Ripos!\r\n"
NBYTES = len(MSG)

# --- section RVAs / raw layout ---
RVA_TEXT = 0x1000
RVA_RDATA = 0x2000
RVA_DATA = 0x3000
RVA_IDATA = 0x4000
raw_text = 0x200
raw_rdata = raw_text + 0x200
raw_data = raw_rdata + 0x200
raw_idata = raw_data + 0x200

# --- .idata layout ---
ID = bytearray()                       # starts at raw_idata (RVA 0x4000)
desc_off = 0
ilt_rva = RVA_IDATA + 0x28
iat_rva = RVA_IDATA + 0x38
# placeholder: descriptor filled later
ID += b"\x00" * 20
ID += b"\x00" * 20                     # null descriptor
ID += u32(0) * 4                       # ILT placeholder (patched below)
ID += u32(0) * 4                       # IAT placeholder (patched below)


def add_name(s):
    global ID
    off = len(ID)
    ID += u16(0) + s + b"\x00"
    return RVA_IDATA + off


hn_getstd = add_name(b"GetStdHandle")
hn_write = add_name(b"WriteFile")
hn_exit = add_name(b"ExitProcess")
# the import descriptor's Name field points at the DLL name STRING directly
# (no hint word, unlike the ILT hint/name entries)
name_k32 = RVA_IDATA + len(ID)
ID += b"kernel32.dll\x00"

# patch ILT and IAT
struct.pack_into("<IIII", ID, 0x28, hn_getstd, hn_write, hn_exit, 0)
struct.pack_into("<IIII", ID, 0x38, hn_getstd, hn_write, hn_exit, 0)
# patch the descriptor
struct.pack_into("<IIIII", ID, desc_off, ilt_rva, 0, 0, name_k32, iat_rva)

# --- code (.text): the -nostdlib-style hello ---
code = bytearray()
# operands are ABSOLUTE guest addresses (ImageBase + RVA)
code += bytes.fromhex("6AF5")                      # push -11  (STD_OUTPUT_HANDLE)
code += b"\xFF\x15" + u32(IMG + iat_rva)           # call [GetStdHandle]  -> eax
# WriteFile(hFile, lpBuffer, nBytes, lpWritten, lpOverlapped): right-to-left
code += bytes.fromhex("6A00")                      # push 0    (lpOverlapped)
code += b"\x68" + u32(IMG + RVA_DATA)              # push written (lpWritten)
code += b"\x6A" + bytes([NBYTES])                  # push nBytes
code += b"\x68" + u32(IMG + RVA_RDATA)             # push msg  (lpBuffer)
code += bytes.fromhex("50")                        # push eax  (hFile)
code += b"\xFF\x15" + u32(IMG + iat_rva + 4)       # call [WriteFile]
code += bytes.fromhex("6A2A")                      # push 42
code += b"\xFF\x15" + u32(IMG + iat_rva + 8)       # call [ExitProcess]

text_raw = bytes(code) + b"\x00" * (0x200 - len(code))
rdata_raw = MSG + b"\x00" * (0x200 - len(MSG))
data_raw = u32(0) + b"\x00" * (0x200 - 4)          # "written" slot

# --- headers ---
dos = bytearray(0x40)
dos[0:2] = b"MZ"
struct.pack_into("<I", dos, 0x3C, 0x40)
pe = bytearray()
pe += b"PE\x00\x00"
pe += u16(0x14C) + u16(4) + u32(0) + u32(0) + u32(0)  # machine, nsec, ts, sym
pe += u16(0xE0) + u16(0x010F)                        # opt size, chars
opt = bytearray(0xE0)
struct.pack_into("<H", opt, 0, 0x10B)                # magic PE32
struct.pack_into("<I", opt, 16, RVA_TEXT)            # AddressOfEntryPoint
struct.pack_into("<I", opt, 28, IMG)                 # ImageBase
struct.pack_into("<I", opt, 32, SEC_ALIGN)
struct.pack_into("<I", opt, 36, FILE_ALIGN)
struct.pack_into("<I", opt, 56, 0x5000)              # SizeOfImage
struct.pack_into("<I", opt, 60, 0x200)               # SizeOfHeaders
struct.pack_into("<H", opt, 68, 3)                   # Subsystem (console)
struct.pack_into("<I", opt, 92, 16)                  # NumberOfRvaAndSizes
struct.pack_into("<II", opt, 96 + 8, RVA_IDATA, 40)  # import directory


def section(name, vsize, vaddr, raw_size, raw_ptr, chars):
    h = bytearray(40)
    h[0:len(name)] = name
    struct.pack_into("<IIIIII", h, 8, vsize, vaddr, raw_size, raw_ptr, 0, 0)
    struct.pack_into("<I", h, 36, chars)
    return h


secs = (
    section(b".text", 0x200, RVA_TEXT, 0x200, raw_text, 0x60000020)
    + section(b".rdata", 0x200, RVA_RDATA, 0x200, raw_rdata, 0x40000040)
    + section(b".data", 0x200, RVA_DATA, 0x200, raw_data, 0xC0000040)
    + section(b".idata", 0x200, RVA_IDATA, 0x200, raw_idata, 0xC0000040)
)
file_img = (bytes(dos) + bytes(pe) + bytes(opt) + bytes(secs))
file_img += b"\x00" * (0x200 - len(file_img))
file_img += text_raw + rdata_raw + data_raw + bytes(ID)
assert len(file_img) == raw_idata + len(ID)

# --- run it through the interpreter ------------------------------------
code, out = peexec.run_pe(file_img)
check("exit code", code == 42, " got %d" % code)
check("stdout", out == MSG, " got %r" % (out,))

# parse-check: peinfo agrees on the imports
pe = peinfo.PeFile(file_img)
dlls = [d.lower() for d, _, _ in pe.imports]
check("peinfo imports", "kernel32.dll" in dlls, " %r" % (dlls,))

print("peexec-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)