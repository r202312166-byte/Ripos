"""boot.py -- M8 boot script: bring the OS up to the interactive shell.

Imported at the end of kernel boot (import boot).  Initializes the
framebuffer and keyboard drivers, starts the Ripos shell, and returns; the
kernel main loop then services the shell's key events.  The shell is the OS
command line: an interactive Python REPL.
"""

import sys
import kern
from framebuffer import Framebuffer
from keyboard import Keyboard
import mouse
import repl

# The user-facing apps live in /apps at the root of the VFS (an
# eye-catching folder); add it to the import path so `import fm`,
# `import editor`, ... work from the shell and from each other.
sys.path.append('/apps')

# The kernel is cooperative (preemption off): hold the GIL continuously so
# long synchronous evals (the file manager / editor construction, scripts)
# never hit CPython's eval-breaker GIL drop/acquire.  That path waits on
# pthread condvar timeouts driven by the kernel timer queue, which is
# timing-sensitive on TCG and occasionally fails to wake the re-acquirer --
# the OS would appear to hang mid-app.  A huge switch interval keeps the
# GIL held for the whole eval; kern.sleep() still releases it explicitly.
sys.setswitchinterval(3600.0)

# --- codec fallbacks -------------------------------------------------
# The frozen encodings package has no filesystem path, so single-byte
# codecs like cp437 (which zipfile needs for ASCII entry names) cannot be
# imported from Lib/encodings.  Register a small cp437 / cp1252 codec here;
# the decoder maps 0x80-0xFF through the real CP437 glyph table, which is
# enough for every standard use on Ripos (filenames, zip/tar metadata).
import codecs

_CP437_GLYPHS = ("\u00c7\u00fc\u00e9\u00e2\u00e4\u00e0\u00e5\u00e7\u00ea\u00eb"
                 "\u00e8\u00ef\u00ee\u00ec\u00c4\u00c5\u00c9\u00e6\u00c6\u00f4"
                 "\u00f6\u00f2\u00fb\u00f9\u00ff\u00d6\u00dc\u00a2\u00a3\u00a5"
                 "\u20a7\u0192\u00e1\u00ed\u00f3\u00fa\u00f1\u00d1\u00aa\u00ba"
                 "\u00bf\u2310\u00ac\u00bd\u00bc\u00a1\u00ab\u00bb\u2591\u2592"
                 "\u2593\u2502\u2524\u2561\u2562\u2556\u2555\u2563\u2551\u2557"
                 "\u255d\u255c\u255b\u2510\u2514\u2534\u252c\u251c\u2500\u253c"
                 "\u255e\u255f\u255a\u2554\u2569\u2566\u2560\u2550\u256c\u2567"
                 "\u2568\u2564\u2565\u2559\u2558\u2552\u2553\u256b\u256a\u2518"
                 "\u250c\u2588\u2584\u258c\u2590\u2580\u03b1\u00df\u0393\u03c0"
                 "\u03a3\u03c3\u00b5\u03c4\u03a6\u0398\u03a9\u03b4\u221e\u03c6"
                 "\u03b5\u2229\u2261\u00b1\u2265\u2264\u2320\u2321\u00f7\u2248"
                 "\u00b0\u2219\u00b7\u221a\u207f\u00b2\u25a0")


def _cp437_decode(data, errors="strict"):
    out = []
    for b in data:
        out.append(chr(b) if b < 0x80 else _CP437_GLYPHS[b - 0x80])
    return "".join(out), len(data)


def _cp437_encode(text, errors="strict"):
    out = bytearray()
    for ch in text:
        o = ord(ch)
        if o < 0x80:
            out.append(o)
        else:
            try:
                out.append(0x80 + _CP437_GLYPHS.index(ch))
            except ValueError:
                if errors == "strict":
                    raise UnicodeEncodeError("cp437", text, len(out), len(out) + 1,
                                             "character not representable")
                out.append(ord("?"))
    return bytes(out), len(text)


def _cp1252_decode(data, errors="strict"):
    out = []
    for b in data:
        if b < 0x80 or 0xA0 <= b:
            out.append(chr(b))
        else:
            # 0x80-0x9F are controls in cp1252; map the common smart quotes
            out.append("\u20ac\ufffd\u201a\u0192\u201e\u2026\u2020\u2021"
                       "\u02c6\u2030\u0160\u2039\u0152\ufffd\u017d\ufffd"
                       "\ufffd\u2018\u2019\u201c\u201d\u2022\u2013\u2014"
                       "\u02dc\u2122\u0161\u203a\u0153\ufffd\u017e\u0178"[b - 0x80])
    return "".join(out), len(data)


def _latin1_decode(data, errors="strict"):
    return "".join(chr(b) for b in data), len(data)


def _find_codec(name):
    name = name.lower()
    if name in ("cp437", "ibm437", "437"):
        return codecs.CodecInfo(_cp437_encode, _cp437_decode, name="cp437")
    if name in ("cp1252", "windows-1252", "1252"):
        return codecs.CodecInfo(_cp1252_decode, None, name="cp1252")
    return None


codecs.register(_find_codec)

kern.write('m8: booting shell\n')
fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)
shell = repl.Shell(fb, kb, mouse=ms)
# The shell is the base keyboard owner: apps that forget to hand the
# keyboard back on quit (e.g. 'import tkdemo' + Quit) restore to this.
import keyboard as _kbd
import appbar
_kbd._BASE = kb
appbar.set_fallback(shell.refresh)
kern.write('m8: shell ready -- boot-to-shell demo (sendkey to type)\n')
kern.write(repl.PROMPT)