# sevenz-test.py -- host gate for initramfs_extra/sevenz.py (M9.3).
# Creates REAL 7z archives with py7zr (LZMA2 default, LZMA1, solid+encoded
# header, empty files) and decodes them with the pure-Python codec.
# Run with host python:  py -3 target/sevenz-test.py

import io
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))

import lzma as _lzma  # noqa: E402
import py7zr  # noqa: E402
import sevenz  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("sevenz-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("sevenz-test FAIL %s%s" % (name, extra))


FILES = {
    "a.txt": b"hello alpha\nline two\n",
    "sub/b.bin": bytes(range(256)) * 8,
    "sub/deep/c.txt": b"deep file content " * 20,
    "empty.txt": b"",
}


def build(tmp, filters=None, encoded_header=False):
    src = os.path.join(tmp, "src%d" % abs(hash((filters is not None, encoded_header)) % 10 ** 6))
    os.makedirs(src, exist_ok=True)
    for name, content in FILES.items():
        p = os.path.join(src, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
    arc = os.path.join(tmp, "t.7z")
    z = py7zr.SevenZipFile(arc, "w", filters=filters)
    if encoded_header:
        z.set_encoded_header_mode(True)
    with z:
        for name in FILES:
            z.write(os.path.join(src, name), name)
    with open(arc, "rb") as f:
        return f.read()


def verify(name, data):
    z = sevenz.SevenZipFile(data)
    names = sorted(n for n in z.namelist() if not n.endswith("/"))
    check(name + " namelist", sorted(names) == sorted(FILES.keys()),
          " got %r" % (names,))
    for fname, content in FILES.items():
        got = z.read(fname)
        check(name + " read " + fname, got == content,
              " len %d vs %d" % (len(got), len(content)))
    # extract_to round trip
    out = os.path.join(tempfile.gettempdir(), "sevenz-extract-" + str(abs(hash(name)) % 10 ** 6))
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    z.extract_to(out)
    ok = True
    for fname, content in FILES.items():
        p = os.path.join(out, fname)
        if not os.path.exists(p) or open(p, "rb").read() != content:
            ok = False
    check(name + " extract_to", ok)
    shutil.rmtree(out)


tmp = tempfile.mkdtemp(prefix="sevenz-test-")

# 1. default LZMA2, non-solid
verify("lzma2", build(tmp))

# 2. LZMA1 filter
verify("lzma1", build(tmp, filters=[{"id": _lzma.FILTER_LZMA1, "preset": 6}]))

# 3. encoded (compressed) header
verify("lzma2-encoded", build(tmp, encoded_header=True))

# 4. LZMA2 with explicit filters (solid folder layout)
verify("lzma2-filters", build(tmp, filters=[{"id": _lzma.FILTER_LZMA2, "preset": 7}]))

# 5. a host-made archive with a directory entry
src = os.path.join(tmp, "src2")
os.makedirs(os.path.join(src, "dir"))
with open(os.path.join(src, "dir", "x.txt"), "w") as f:
    f.write("in dir")
arc = os.path.join(tmp, "d.7z")
with py7zr.SevenZipFile(arc, "w") as z:
    z.write(os.path.join(src, "dir", "x.txt"), "dir/x.txt")
with open(arc, "rb") as f:
    d = f.read()
z = sevenz.SevenZipFile(d)
check("lzma2-dir namelist", z.namelist() == ["dir/x.txt"], " got %r" % (z.namelist(),))
check("lzma2-dir read", z.read("dir/x.txt") == b"in dir", " got %r" % (z.read("dir/x.txt"),))

# 6. bad signature rejected
try:
    sevenz.SevenZipFile(b"not a 7z file at all")
    check("bad sig", False)
except sevenz.SevenZipError:
    check("bad sig", True)

shutil.rmtree(tmp)
print("sevenz-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
