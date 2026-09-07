# clipboard.py -- M9.6 global text clipboard for Ripos apps.
#
# The OS has no window manager clipboard, so apps share a single module-level
# text buffer.  Wired into the editor (Ctrl+C/V/X) and available to tk.Entry
# and the shell via tk's <Control-*> hotkey pre-pass.

_buffer = ""


def copy(text):
    global _buffer
    _buffer = "" if text is None else str(text)
    return _buffer


def cut(text):
    global _buffer
    _buffer = "" if text is None else str(text)
    return ""


def paste():
    return _buffer


def get():
    return _buffer


def clear():
    copy("")
