# rtf-docx-test.py -- host gate for initramfs_extra/rtf.py + docx.py (M9.4).
import io
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
import docx  # noqa: E402
import rtf   # noqa: E402
import zipfile  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("rtf-docx-test PASS %s%s" % (name, extra))
    else:
        FAIL += 1
        print("rtf-docx-test FAIL %s%s" % (name, extra))


# ---- RTF --------------------------------------------------------------
text = "Hello, Ripos!\nLine two with a tab\there.\nUnicode: caf\u00e9 \u4e2d\u6587"
enc = rtf.save_rtf(text)
dec = rtf.load_rtf(enc)
check("rtf roundtrip", dec == text, " got %r" % (dec,))
check("rtf unicode escapes", b"\\u233?" in enc and b"\\u20013?" in enc and b"\\u25991?" in enc,
      " enc %r" % (enc[:140],))

# crafted RTF with escapes and control words
crafted = (
    b"{\\rtf1\\ansi {\\fonttbl{\\f0 Times;}}"
    b"{\\f0\\b bold text\\b0 and normal\\par"
    b"escapes: \\{ \\} \\\\ \\'e9 \\u23383?\\par"
    b"{\\*\\field{skip me}}keep this\\par"
    b"}"
)
got = rtf.load_rtf(crafted)
check("rtf bold dropped", "bold textand normal" in got, " got %r" % (got,))
check("rtf escape chars", "{" in got and "}" in got and "\\" in got, " got %r" % (got,))
check("rtf hex e-acute", "\u00e9" in got, " got %r" % (got,))
check("rtf unicode U+5B57", "\u5b57" in got, " got %r" % (got,))
check("rtf skip dest", "keep this" in got, " got %r" % (got,))

# CJK RTF: \'hh pairs decoded with the \ansicpg codepage, font names skipped
def esc_body(s, cp):
    return b"".join(b"\\'%02x" % b for b in s.encode(cp))

cjk_pairs = (
    b"{\\rtf1\\ansi\\ansicpg936 {\\fonttbl{\\f0 " + esc_body("\u5b8b\u4f53", "gbk") + b";}}"
    b"{\\f0 " + esc_body("\u4e2d\u6587\u6d4b\u8bd5 abc", "gbk") + b"\\par}"
)
gc = rtf.load_rtf(cjk_pairs)
check("rtf CJK \\'hh pairs", gc == "\u4e2d\u6587\u6d4b\u8bd5 abc\n", " got %r" % (gc,))
check("rtf font name skipped", "\u5b8b\u4f53" not in gc, " got %r" % (gc,))
# raw GBK body (ansicpg54936) + trailing NUL
cjk_raw = (
    b"{\\rtf1\\ansi\\ansicpg54936 {\\fonttbl{\\f0 \\'cb\\'ce\\'cc\\'e5;}}"
    b"\\f0 " + "\u73cd\u8d35".encode("gbk") + b"\\par}\r\n\x00"
)
gr = rtf.load_rtf(cjk_raw)
check("rtf raw GBK body", gr == "\u73cd\u8d35\n", " got %r" % (gr,))
check("rtf trailing NUL stripped", "\x00" not in gr, "")

# ---- DOCX -------------------------------------------------------------
dtext = "alpha\nbeta gamma\n\nlast line"
dx = docx.save_docx(dtext)
check("docx is a zip", zipfile.is_zipfile(io.BytesIO(dx)), "")
with zipfile.ZipFile(io.BytesIO(dx)) as z:
    check("docx parts", "word/document.xml" in z.namelist() and "[Content_Types].xml" in z.namelist(), "")
    ET.fromstring(z.read("word/document.xml"))
check("docx xml parses", True)
back = docx.load_docx(dx)
check("docx roundtrip", back == dtext, " got %r" % (back,))
du = docx.save_docx("\u4e2d\u6587 and \u00e9")
check("docx unicode", docx.load_docx(du) == "\u4e2d\u6587 and \u00e9",
      " got %r" % (docx.load_docx(du),))

print("rtf-docx-test: %d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
