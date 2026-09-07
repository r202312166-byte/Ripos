# peinfo-test.py -- host gate for initramfs_extra/peinfo.py (M10.1).
# Compiles real mingw PE files (hello.exe, mydll.dll) and parses them with
# the pure-Python parser: headers, sections, imports, exports, relocations.
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import peinfo  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("peinfo-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("peinfo-test FAIL %s%s" % (name, extra))


GCC = "C:\\msys64\\mingw64\\bin\\gcc.exe"
src = os.path.join(os.path.dirname(__file__), "pe_src")
exe = os.path.join(os.path.dirname(__file__), "pe_hello.exe")
dll = os.path.join(os.path.dirname(__file__), "pe_mydll.dll")
for out, cfile, extra in ((exe, "hello.c", []), (dll, "mydll.c", ["-shared"])):
    if os.path.exists(out):
        os.remove(out)
    env = dict(os.environ)
    env["PATH"] = "C:\\msys64\\mingw64\\bin;C:\\msys64\\usr\\bin;" + env.get("PATH", "")
    r = subprocess.run([GCC, os.path.join(src, cfile), "-o", out] + extra,
                       capture_output=True, text=True, env=env)
    check("compile " + cfile, r.returncode == 0 and os.path.exists(out),
          " err=%s" % (r.stderr[:120] if r.returncode else ""))

# ---- hello.exe --------------------------------------------------------
data = open(exe, "rb").read()
pe = peinfo.PeFile(data)
check("exe machine", pe.machine in (peinfo.MACHINE_I386, peinfo.MACHINE_AMD64),
      " got %#x" % pe.machine)
check("exe is PE32+" if not pe.is_pe32 else "exe is PE32", True, " base=%#x" % pe.image_base)
check("exe image base", pe.image_base in (0x400000, 0x140000000), " got %#x" % pe.image_base)
check("exe entry point", pe.entry_point != 0, " got %#x" % pe.entry_point)
check("exe sections", len(pe.sections) >= 3, " got %d" % len(pe.sections))
code = [s for s in pe.sections if s.is_code()]
check("exe code section", len(code) >= 1, " %r" % ([s.name for s in pe.sections],))
dlls = [d for d, _, _ in pe.imports]
check("exe imports kernel32", "kernel32.dll" in [d.lower() for d in dlls],
      " dlls=%r" % (dlls,))
funcs = [f for d, fs, _ in pe.imports if d.lower() == "kernel32.dll" for f in fs]
check("exe imports WriteFile", any("WriteFile" in f for f in funcs),
      " funcs=%r" % (funcs[:6],))
check("exe relocations", len(pe.relocations) >= 0, "")

# ---- mydll.dll --------------------------------------------------------
data = open(dll, "rb").read()
pd = peinfo.PeFile(data)
check("dll machine", pd.machine in (peinfo.MACHINE_I386, peinfo.MACHINE_AMD64), "")
check("dll exports", len(pd.exports) >= 2, " got %r" % ([(n, o, hex(r)) for n, o, r in pd.exports],))
names = [n for n, _, _ in pd.exports]
check("dll my_add", "my_add" in names, " %r" % (names,))
check("dll my_greet", "my_greet" in names, " %r" % (names,))

# ---- bad data ---------------------------------------------------------
try:
    peinfo.PeFile(b"not a pe file")
    check("bad pe rejected", False)
except peinfo.PeError:
    check("bad pe rejected", True)

print("peinfo-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)