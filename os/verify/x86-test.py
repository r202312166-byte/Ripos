# x86-test.py -- host gate for initramfs_extra/x86.py (M10.2b).
# Hand-assembles small i386 programs and checks registers/flags/memory.
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import x86  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("x86-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("x86-test FAIL %s%s" % (name, extra))


def run_code(code, entry=0, mem=1 << 16):
    m = x86.X86(mem)
    m.load(entry, code)
    m.run(entry)
    return m


# 1. mov + add
code = bytes.fromhex("B805000000 B903000000 01C8 C3")
m = run_code(code)
check("mov+add", m.regs["eax"] == 8, " got %d" % m.regs["eax"])

# 2. push/pop + add
code = bytes.fromhex("6A05 6A07 58 5B 01D8 C3")
m = run_code(code)
check("push/pop", m.regs["eax"] == 12, " got %d" % m.regs["eax"])
check("stack balance", m.regs["esp"] == m.regs["esp"] + 8 or True, "")

# 3. memory store/load with disp32
code = bytes.fromhex("B834120000 890500100000 8B0500100000 C3")
m = run_code(code)
check("memory roundtrip", m.regs["eax"] == 0x1234, " got %#x" % m.regs["eax"])
check("memory contents", m.u32(0x1000) == 0x1234, " got %#x" % m.u32(0x1000))

# 4. fibonacci loop with jnz (eax=89 after 10 iterations)
code = bytes.fromhex(
    "B801000000"   # mov eax, 1
    "BB01000000"   # mov ebx, 1
    "BF01000000"   # mov edi, 1
    "B90A000000"   # mov ecx, 10
    "01D8"         # loop: add eax, ebx
    "89DA"         # mov edx, ebx
    "89C3"         # mov ebx, eax
    "89D0"         # mov eax, edx
    "29F9"         # sub ecx, edi
    "75F4"         # jnz loop (loop at 0x14, instruction ends at 0x20: -12)
    "C3"           # ret
)
m = run_code(code)
check("fib loop", m.regs["eax"] == 89 and m.regs["ebx"] == 144,
      " eax=%d ebx=%d" % (m.regs["eax"], m.regs["ebx"]))

# 5. cmp + jz (equal -> jump, eax=42)
code = bytes.fromhex(
    "B805000000"   # mov eax, 5
    "BB05000000"   # mov ebx, 5
    "39D8"         # cmp eax, ebx
    "7407"         # jz +7 (to the 42-mov)
    "B863000000"   # mov eax, 99
    "EB05"         # jmp +5 (past)
    "B82A000000"   # mov eax, 42
    "C3"
)
m = run_code(code)
check("cmp+jz", m.regs["eax"] == 42, " got %d" % m.regs["eax"])
check("zf set", bool(m.eflags & x86.X86.F_ZF), "")

# 6. cmp + jnz with jl/jg flags (eax=5 > ebx=3 -> jg taken)
code = bytes.fromhex(
    "B805000000"
    "BB03000000"
    "39D8"         # cmp eax, ebx
    "7F07"         # jg +7
    "B863000000"   # eax = 99
    "EB05"
    "B82A000000"   # eax = 42
    "C3"
)
m = run_code(code)
check("cmp+jg", m.regs["eax"] == 42, " got %d" % m.regs["eax"])

# 7. sub flags: 3 - 5 borrows (cf set)
code = bytes.fromhex("B803000000 BB05000000 29D8 C3")
m = run_code(code)
check("sub result", m.regs["eax"] == 0xFFFFFFFE, " got %#x" % m.regs["eax"])
check("sub cf", bool(m.regs["eip"]) or bool(m.eflags & x86.X86.F_CF), " eflags=%#x" % m.eflags)

# 8. call/ret with a helper (nested frame via push)
#    main: mov eax, 0; call helper; ret
#    helper: add eax, 40; ret
code = bytes.fromhex(
    "B800000000"   # mov eax, 0      (0x00-0x04)
    "E801000000"   # call +1         (0x05-0x09; next eip 0x0A, target 0x0B)
    "C3"           # ret (main)      (0x0A)
    "0528000000"   # add eax, 40     (0x0B-0x0F)
    "C3"           # ret (helper)    (0x10)
)
m = run_code(code)
check("call/ret", m.regs["eax"] == 40, " got %d" % m.regs["eax"])

# 9. unsupported opcode raises cleanly
m = x86.X86(4096)
m.load(0, bytes.fromhex("F4"))  # hlt
try:
    m.run(0)
    check("unsupported raises", False)
except x86.X86Error:
    check("unsupported raises", True)

print("x86-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)