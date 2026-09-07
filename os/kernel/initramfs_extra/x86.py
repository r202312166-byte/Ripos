# x86.py -- pure-Python i386 interpreter for Ripos (M10.2b).
#
# The "interpretive OS" heart: a small 32-bit x86 (i386) machine interpreter
# that runs guest code over a flat bytearray memory.  Implements the subset
# the plan calls for -- mov/add/sub/cmp/and/or/xor/lea/push/pop/call/ret/jcc
# + EFLAGS -- which is enough for small hand-assembled console programs
# (and, later, the M10 interpreter backend for .exe files).
#
# API:  X86(memsize) -> .load(addr, bytes) .run(entry, steps) .regs .mem
#       .read32(addr) .write32(addr, val) .push32/.pop32

import struct


class X86Halt(Exception):
    """Raised by run() when the program stops (ret from the start frame)."""

    def __init__(self, ret=None):
        self.ret = ret
        super().__init__("halt")


class X86Error(Exception):
    pass


class X86:
    R32 = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
    # EFLAGS bits
    F_CF = 1
    F_PF = 4
    F_ZF = 0x40
    F_SF = 0x80
    F_OF = 0x800

    def __init__(self, memsize=1 << 20):
        self.mem = bytearray(memsize)
        self.regs = {n: 0 for n in self.R32}
        self.regs["esp"] = memsize - 8
        self.eflags = 0
        self.steps = 0
        self.shims = {}   # guest addr -> (fn(machine, args), nargs, clean)
        self.end_marker = 0xFFFFFFFF  # top-level ret pops this and halts

    def register_shim(self, addr, fn, nargs, clean):
        self.shims[addr] = (fn, nargs, clean)

    def _call_shim(self, target):
        # stdcall: the guest stack holds the return address then the args;
        # the callee cleans (clean) bytes.  Returns the next eip.
        fn, nargs, clean = self.shims[target]
        base = self.regs["esp"]
        args = [self.u32(base + 4 + 4 * i) for i in range(nargs)]
        fn(self, args)
        ret = self.u32(base)
        self.regs["esp"] = (base + 4 + clean) & 0xFFFFFFFF
        return ret

    # ---- memory helpers ----------------------------------------------

    def load(self, addr, data):
        self.mem[addr:addr + len(data)] = data

    def u8(self, a):
        return self.mem[a]

    def u16(self, a):
        return struct.unpack_from("<H", self.mem, a)[0]

    def u32(self, a):
        return struct.unpack_from("<I", self.mem, a)[0]

    def w32(self, a, v):
        struct.pack_into("<I", self.mem, a, v & 0xFFFFFFFF)

    def push32(self, v):
        self.regs["esp"] = (self.regs["esp"] - 4) & 0xFFFFFFFF
        self.w32(self.regs["esp"], v)

    def pop32(self):
        v = self.u32(self.regs["esp"])
        self.regs["esp"] = (self.regs["esp"] + 4) & 0xFFFFFFFF
        return v

    # ---- flags -------------------------------------------------------

    def _set_flags_arith(self, res, a, b, is_add):
        m = 0xFFFFFFFF
        res &= m
        self.eflags &= ~(self.F_CF | self.F_PF | self.F_ZF | self.F_SF | self.F_OF)
        if res == 0:
            self.eflags |= self.F_ZF
        if res & 0x80000000:
            self.eflags |= self.F_SF
        # parity (low byte)
        b8 = res & 0xFF
        p = bin(b8).count("1") & 1
        if not p:
            self.eflags |= self.F_PF
        if is_add:
            # carry: unsigned overflow
            if res < (a & m):
                self.eflags |= self.F_CF
            # signed overflow: signs of a,b differ from result
            if ((a ^ res) & (b ^ res)) & 0x80000000:
                self.eflags |= self.F_OF
        else:
            # sub: carry = borrow (a < b unsigned)
            if (a & m) < (b & m):
                self.eflags |= self.F_CF
            if ((a ^ b) & (a ^ res)) & 0x80000000:
                self.eflags |= self.F_OF
        return res

    def _set_flags_logic(self, res):
        res &= 0xFFFFFFFF
        self.eflags &= ~(self.F_PF | self.F_ZF | self.F_SF | self.F_CF | self.F_OF)
        if res == 0:
            self.eflags |= self.F_ZF
        if res & 0x80000000:
            self.eflags |= self.F_SF
        if not (bin(res & 0xFF).count("1") & 1):
            self.eflags |= self.F_PF

    # ---- condition codes ---------------------------------------------

    def _cond(self, cc):
        f = self.eflags
        if cc == 0x0:   # jo
            return bool(f & self.F_OF)
        if cc == 0x1:   # jno
            return not (f & self.F_OF)
        if cc == 0x2:   # jb/jc
            return bool(f & self.F_CF)
        if cc == 0x3:   # jnb
            return not (f & self.F_CF)
        if cc == 0x4:   # jz/je
            return bool(f & self.F_ZF)
        if cc == 0x5:   # jnz/jne
            return not (f & self.F_ZF)
        if cc == 0x6:   # jbe
            return bool(f & (self.F_CF | self.F_ZF))
        if cc == 0x7:   # ja
            return not (f & (self.F_CF | self.F_ZF))
        if cc == 0x8:   # js
            return bool(f & self.F_SF)
        if cc == 0x9:   # jns
            return not (f & self.F_SF)
        if cc == 0xA:   # jp
            return bool(f & self.F_PF)
        if cc == 0xB:   # jnp
            return not (f & self.F_PF)
        if cc == 0xC:   # jl
            return ((f & self.F_SF) != 0) != ((f & self.F_OF) != 0)
        if cc == 0xD:   # jge
            return ((f & self.F_SF) != 0) == ((f & self.F_OF) != 0)
        if cc == 0xE:   # jle
            return (f & self.F_ZF) or (((f & self.F_SF) != 0) != ((f & self.F_OF) != 0))
        if cc == 0xF:   # jg
            return not (f & self.F_ZF) and ((f & self.F_SF) != 0) == ((f & self.F_OF) != 0)
        return False

    # ---- modrm -------------------------------------------------------

    def _modrm(self, eip):
        """Decode modrm; return (mod, reg, rm, operand_address_or_None)."""
        b = self.u8(eip)
        mod = (b >> 6) & 3
        reg = (b >> 3) & 7
        rm = b & 7
        disp = 0
        p = eip + 1
        addr = None
        base = rm
        index = None
        scale = 0
        if rm == 4 and mod != 3:
            # SIB byte: scale(2) index(3) base(3)
            sib = self.u8(p)
            p += 1
            scale = (sib >> 6) & 3
            index = (sib >> 3) & 7
            base = sib & 7
        if mod == 0 and rm == 5 and not (rm == 4 and base == 5):
            disp = self.u32(p)
            p += 4
            addr = disp
        else:
            if mod == 1:
                disp = struct.unpack_from("<b", self.mem, p)[0]
                p += 1
            elif mod == 2:
                disp = self.u32(p)
                p += 4
            if mod == 3:
                addr = None
            else:
                a = 0
                if rm == 4:
                    if base == 5 and mod == 0:
                        base = None  # disp32 base (handled above)
                    if base is not None:
                        a += self.regs[self.R32[base]]
                    if index != 4:  # esp index = no index
                        a += self.regs[self.R32[index]] << scale
                else:
                    a += self.regs[self.R32[rm]]
                addr = (a + disp) & 0xFFFFFFFF
        return mod, reg, rm, addr, p

    # ---- execution ---------------------------------------------------

    def run(self, entry, steps=1_000_000):
        self.regs["eip"] = entry
        self.push32(self.end_marker)  # a bare top-level ret halts here
        try:
            while self.steps < steps:
                self.steps += 1
                self._step()
        except X86Halt as h:
            return h.ret
        raise X86Error("step limit exceeded")

    def _step(self):
        eip = self.regs["eip"]
        op = self.u8(eip)
        eip += 1
        if op == 0x90:                      # nop
            pass
        elif 0x50 <= op <= 0x57:            # push r32
            self.push32(self.regs[self.R32[op - 0x50]])
        elif 0x58 <= op <= 0x5F:            # pop r32
            self.regs[self.R32[op - 0x58]] = self.pop32()
        elif 0xB8 <= op <= 0xBF:            # mov r32, imm32
            self.regs[self.R32[op - 0xB8]] = self.u32(eip)
            eip += 4
        elif op == 0x68:                    # push imm32
            self.push32(self.u32(eip))
            eip += 4
        elif op == 0x6A:                    # push imm8 (sign-extended)
            v = struct.unpack_from("<b", self.mem, eip)[0]
            self.push32(v)
            eip += 1
        elif op == 0xC3:                    # ret
            eip = self._ret(0)
        elif op == 0xC2:                    # ret imm16
            eip = self._ret(self.u16(eip))
        elif op == 0xEB:                    # jmp rel8
            eip = (eip + 1 + struct.unpack_from("<b", self.mem, eip)[0]) & 0xFFFFFFFF
        elif op == 0xE9:                    # jmp rel32
            eip = (eip + 4 + self.u32(eip)) & 0xFFFFFFFF
        elif op == 0xE8:                    # call rel32
            rel = self.u32(eip)
            eip += 4
            self.push32(eip)
            eip = (eip + rel) & 0xFFFFFFFF
        elif 0x70 <= op <= 0x7F:            # jcc rel8
            if self._cond(op & 0xF):
                eip = (eip + 1 + struct.unpack_from("<b", self.mem, eip)[0]) & 0xFFFFFFFF
            else:
                eip += 1
        elif op == 0x0F:                    # two-byte
            op2 = self.u8(eip)
            eip += 1
            if 0x80 <= op2 <= 0x8F:         # jcc rel32
                rel = self.u32(eip)
                eip += 4
                if self._cond(op2 & 0xF):
                    eip = (eip + rel) & 0xFFFFFFFF
            else:
                raise X86Error("unsupported 0F opcode %02x" % op2)
        elif op in (0x05, 0x2D, 0x25, 0x0D, 0x35, 0x3D):
            # accumulator-immediate: add/sub/and/or/xor/cmp eax, imm32
            imm = self.u32(eip)
            eip += 4
            lhs = self.regs["eax"]
            if op == 0x05:
                self.regs["eax"] = self._set_flags_arith(lhs + imm, lhs, imm, True)
            elif op == 0x2D:
                self.regs["eax"] = self._set_flags_arith(lhs - imm, lhs, imm, False)
            elif op == 0x25:
                self.regs["eax"] = lhs & imm
                self._set_flags_logic(self.regs["eax"])
            elif op == 0x0D:
                self.regs["eax"] = lhs | imm
                self._set_flags_logic(self.regs["eax"])
            elif op == 0x35:
                self.regs["eax"] = lhs ^ imm
                self._set_flags_logic(self.regs["eax"])
            else:
                self._set_flags_arith(lhs - imm, lhs, imm, False)
        elif op in (0x88, 0x89, 0x8A, 0x8B, 0x8D, 0xC7) or op in (
                0x01, 0x03, 0x29, 0x2B, 0x21, 0x23, 0x09, 0x0B,
                0x31, 0x33, 0x39, 0x3B, 0x8B, 0x8A, 0x88, 0x89):
            eip = self._alu_or_mov(op, eip)
        elif op == 0xFF:
            # FF /2 call r/m32, FF /5 jmp r/m32 (indirect)
            mod, reg, rm, addr, p = self._modrm(eip)
            if mod == 3:
                target = self.regs[self.R32[rm]]
            else:
                target = self.u32(addr)
            eip = p
            if reg == 2:      # call
                self.push32(eip)   # return address (needed by shims too)
                if target in self.shims:
                    eip = self._call_shim(target)
                else:
                    eip = target & 0xFFFFFFFF
            elif reg == 5:    # jmp
                eip = target & 0xFFFFFFFF
            else:
                raise X86Error("unsupported FF /%d" % reg)
        elif op == 0xCC:
            raise X86Error("int3")
        else:
            raise X86Error("unsupported opcode %02x at %08x" % (op, self.regs["eip"] - 1))
        self.regs["eip"] = eip

    def _alu_or_mov(self, op, eip):
        mod, reg, rm, addr, p = self._modrm(eip)
        rname = self.R32[reg]
        if mod == 3:
            rmname = self.R32[rm]
        if op == 0x8B or op == 0x8A:        # mov r, r/m32
            v = self.regs[rmname] if mod == 3 else self.u32(addr)
            self.regs[rname] = v
            return p
        if op == 0x89 or op == 0x88:        # mov r/m32, r
            v = self.regs[rname]
            if mod == 3:
                self.regs[rmname] = v
            else:
                self.w32(addr, v)
            return p
        if op == 0x8D:                      # lea r, m
            self.regs[rname] = addr
            return p
        if op == 0xC7:                      # mov r/m32, imm32
            imm = self.u32(p)
            p += 4
            if mod == 3:
                self.regs[rmname] = imm
            else:
                self.w32(addr, imm)
            return p
        # arithmetic: op 01/03 add, 29/2B sub, 21/23 and, 09/0B or, 31/33 xor, 39/3B cmp
        table = {
            0x01: ("add", 0), 0x03: ("add", 1),
            0x29: ("sub", 0), 0x2B: ("sub", 1),
            0x21: ("and", 0), 0x23: ("and", 1),
            0x09: ("or", 0), 0x0B: ("or", 1),
            0x31: ("xor", 0), 0x33: ("xor", 1),
            0x39: ("cmp", 0), 0x3B: ("cmp", 1),
        }
        kind, direction = table[op]
        lhs = self.regs[rname] if direction else (self.regs[rmname] if mod == 3 else self.u32(addr))
        rhs = (self.regs[rmname] if mod == 3 else self.u32(addr)) if direction else self.regs[rname]
        if kind == "add":
            res = self._set_flags_arith(lhs + rhs, lhs, rhs, True)
        elif kind == "sub":
            res = self._set_flags_arith(lhs - rhs, lhs, rhs, False)
        elif kind == "and":
            res = lhs & rhs
            self._set_flags_logic(res)
        elif kind == "or":
            res = lhs | rhs
            self._set_flags_logic(res)
        elif kind == "xor":
            res = lhs ^ rhs
            self._set_flags_logic(res)
        else:  # cmp: like sub but no store
            self._set_flags_arith(lhs - rhs, lhs, rhs, False)
            return p
        if direction:
            self.regs[rname] = res
        elif mod == 3:
            self.regs[rmname] = res
        else:
            self.w32(addr, res)
        return p

    def _ret(self, extra):
        ret = self.pop32()
        self.regs["esp"] = (self.regs["esp"] + extra) & 0xFFFFFFFF
        if ret == self.end_marker:
            raise X86Halt(0)
        return ret