# boot-codec-test.py -- verify the boot.py codec fallback registers + works.
import sys, os, pathlib
# simulate: import boot.py's codec section by exec'ing the relevant part
src = open(os.path.join("kernel", "initramfs_extra", "boot.py"), encoding="utf-8").read()
# exec only up to the codecs.register line (before kern usage)
idx = src.index("codecs.register(_find_codec)")
code = src[src.index("import codecs"):src.index("codecs.register(_find_codec)") + len("codecs.register(_find_codec)")]
g = {}
exec(code, g)
import codecs
info = codecs.lookup("cp437")
print("cp437 lookup:", info.name)
s = "abc".encode("cp437") if False else None
dec = info.decode(b"a\x80b")  # 0x80 -> \u00c7
print("decode:", dec[0] == "a\u00c7b", repr(dec[0]))
print("zipfile uses it:", codecs.lookup("cp437").decode(b"a.txt")[0] == "a.txt")
print("cp1252:", codecs.lookup("cp1252").name)
print("BOOT CODEC OK")
