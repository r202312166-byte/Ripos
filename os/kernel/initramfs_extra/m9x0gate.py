# m9x0gate.py -- M9.0 gate: the writable /home RAM disk.
# Run from the M8 shell:  import m9x0gate
# Exercises create/write/append/read, mkdir/listdir, rename, stat,
# remove/rmdir, the read-only initramfs boundary, truncate and
# exec-from-disk (the editor's save path).  All markers go to serial.

import os
import sys


def check(name, cond, extra=""):
    sys.stdout.write("m9x0 %s %s%s\n" % ("PASS" if cond else "FAIL", name, extra))
    sys.stdout.flush()


# 1. create + write + close + read back
f = open("/home/g.txt", "w")
f.write("M9.0 ok")
f.close()
got = open("/home/g.txt").read()
check("write+read", got == "M9.0 ok", " got=%r" % got)

# 2. append mode
f = open("/home/g.txt", "a")
f.write("!")
f.close()
got = open("/home/g.txt").read()
check("append", got == "M9.0 ok!", " got=%r" % got)

# 3. mkdir + listdir
os.mkdir("/home/sub")
check("mkdir", os.path.isdir("/home/sub"))
entries = sorted(os.listdir("/home"))
check("listdir", "g.txt" in entries and "sub" in entries, " entries=%r" % entries)

# 4. rename into the subdir
os.rename("/home/g.txt", "/home/sub/g2.txt")
check(
    "rename",
    os.path.exists("/home/sub/g2.txt") and not os.path.exists("/home/g.txt"),
)

# 5. stat / getsize
sz = os.path.getsize("/home/sub/g2.txt")
check("getsize", sz == 8, " size=%d" % sz)

# 6. remove + rmdir (the boot fixtures stay in /home; only the gate's
# own files must be gone)
os.remove("/home/sub/g2.txt")
os.rmdir("/home/sub")
left = os.listdir("/home")
check("remove+rmdir", "g.txt" not in left and "sub" not in left,
      " left=%r" % left)

# 7. initramfs stays read-only (the RAM disk is only under /home)
try:
    open("/etc/motd", "w").write("nope")
    check("ro-initramfs", False, " (wrote to /etc/motd!)")
except OSError as e:
    check("ro-initramfs", True, " errno=%s" % getattr(e, "errno", "?"))

# 8. exec-from-disk: write a python file, then exec it (editor save path)
with open("/home/hello.py", "w") as f:
    f.write("print('hello from disk')\n")
ns = {}
exec(open("/home/hello.py").read(), ns)
check("exec-from-disk", True)

# 9. os.truncate
with open("/home/t.bin", "wb") as f:
    f.write(b"0123456789")
os.truncate("/home/t.bin", 4)
check("truncate", os.path.getsize("/home/t.bin") == 4, " size=%d" % os.path.getsize("/home/t.bin"))
os.remove("/home/t.bin")

# 10. /home shows up in the root listing and is a directory
check("home-in-root", "home" in os.listdir("/"))
check("home-mount", os.path.isdir("/home"))

sys.stdout.write("m9x0 GATE DONE\n")
sys.stdout.flush()
