# peexec.py -- run a PE32 in the pure-Python x86 interpreter (M10, Method B).
#
# The "interpretive OS" executable path: peinfo parses the PE, peexec maps
# its sections into the interpreter's flat guest memory (ImageBase + RVA),
# resolves the import table (ILT/IAT) to Python API shims, then runs the
# entry point.  Win32 stdcall shims get the guest stack arguments and the
# machine, and return through the normal call/ret discipline.
#
# API:  runner = PeRunner(); runner.add_shim("kernel32.dll", "WriteFile",
#        shim_fn, nargs, clean); code = runner.run(pe_bytes)

import peinfo
import x86

SHIM_BASE = 0xFF000000


class PeRunner:
    """Loads and runs a PE32 in an X86 interpreter with Python shims."""

    def __init__(self, memsize=1 << 29):  # 512 MiB: exe + DLL bases + stack
        self.m = x86.X86(memsize)
        self.stdout = bytearray()
        self.exit_code = 0
        self.shims = {}       # (dll_lower, name) -> fn
        self.shim_addrs = {}  # (dll_lower, name) -> guest addr
        self._next_addr = SHIM_BASE
        self.dlls = {}    # dll name (lower) -> {export name: guest addr}
        # default Win32 shims
        self.add_shim("kernel32.dll", "GetStdHandle", self._GetStdHandle, 1, 4)
        self.add_shim("kernel32.dll", "WriteFile", self._WriteFile, 5, 20)
        self.add_shim("kernel32.dll", "ExitProcess", self._ExitProcess, 1, 0)

    # -- shim registration ---------------------------------------------

    def add_shim(self, dll, name, fn, nargs, clean):
        key = (dll.lower(), name)
        addr = self._next_addr
        self._next_addr += 4
        self.shims[key] = fn
        self.shim_addrs[key] = addr

        def invoke(machine, args):
            fn(machine, args)

        self.m.register_shim(addr, invoke, nargs, clean)

    # -- default Win32 shims --------------------------------------------

    def _GetStdHandle(self, machine, args):
        # nStdHandle: -11 = STD_OUTPUT_HANDLE -> return a fake handle
        machine.regs["eax"] = 1

    def _WriteFile(self, machine, args):
        h, buf, n, written, ovl = args
        data = bytes(machine.mem[buf:buf + n])
        self.stdout += data
        if written:
            machine.w32(written, n)
        machine.regs["eax"] = 1  # TRUE

    def _ExitProcess(self, machine, args):
        self.exit_code = args[0] & 0xFF
        raise x86.X86Halt(self.exit_code)

    def load_dll(self, data, name):
        """Map a DLL into guest memory; resolves its own imports (to shims)
        and records its exports as {export_name: guest_address}."""
        pe = peinfo.PeFile(data)
        if pe.is_pe32 is not True:
            raise peinfo.PeError("interpreter backend supports PE32 (i386) only")
        m = self.m
        for s in pe.sections:
            dst = pe.image_base + s.vaddr
            if s.raw_size:
                m.load(dst, data[s.raw_ptr:s.raw_ptr + s.raw_size])
            if s.raw_size < s.vsize:
                m.load(dst + s.raw_size, bytes(s.vsize - s.raw_size))
        for dll, funcs, iat_slots in pe.imports:
            for fname, slot in zip(funcs, iat_slots):
                key = (dll.lower(), fname)
                if key in self.shim_addrs:
                    m.w32(pe.image_base + slot, self.shim_addrs[key])
                else:
                    raise peinfo.PeError("no shim for %s!%s" % (dll, fname))
        exports = {}
        for (ename, _ord, rva) in pe.exports:
            if ename:
                exports[ename] = (pe.image_base + rva) & 0xFFFFFFFF
        self.dlls[name.lower()] = exports
        return exports

    # -- loading --------------------------------------------------------

    def run(self, data):
        pe = peinfo.PeFile(data)
        if pe.is_pe32 is not True:
            raise peinfo.PeError("interpreter backend supports PE32 (i386) only")
        m = self.m
        # map sections at ImageBase + RVA, zero-fill beyond raw data
        for s in pe.sections:
            dst = pe.image_base + s.vaddr
            if s.raw_size:
                m.load(dst, data[s.raw_ptr:s.raw_ptr + s.raw_size])
            if s.raw_size < s.vsize:
                m.load(dst + s.raw_size, bytes(s.vsize - s.raw_size))
        # resolve imports: shim addresses or loaded-DLL export addresses
        for dll, funcs, iat_slots in pe.imports:
            exports = self.dlls.get(dll.lower())
            for name, slot in zip(funcs, iat_slots):
                if exports and name in exports:
                    m.w32(pe.image_base + slot, exports[name])
                else:
                    key = (dll.lower(), name)
                    if key in self.shim_addrs:
                        m.w32(pe.image_base + slot, self.shim_addrs[key])
                    else:
                        raise peinfo.PeError("no shim for %s!%s" % (dll, name))
        try:
            m.run(pe.image_base + pe.entry_point)
        except x86.X86Halt as h:
            if h.ret is not None and self.exit_code == 0:
                self.exit_code = h.ret & 0xFF
        return self.exit_code


def run_pe(data, memsize=1 << 29):
    """Convenience: run a PE with the default Win32 shims.
    Returns (exit_code, stdout_bytes)."""
    r = PeRunner(memsize)
    code = r.run(data)
    return code, bytes(r.stdout)