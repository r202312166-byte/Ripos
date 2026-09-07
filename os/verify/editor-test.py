# editor-test.py -- host gate for the M9 ISE-style editor (no OS boot).
# Stubs kern, runs the real editor.Editor on a fake framebuffer, and
# asserts: typing/editing (insert, newline, backspace, delete, arrows),
# clicking to place the cursor, F5 running the buffer with output
# captured in the bottom console pane, pane line evaluation with a
# persistent namespace, F6 pane switching, and Esc quitting.

import pathlib
import sys


class _Kern:
    def __init__(self):
        self.w = 640
        self.h = 480
        self.bpp = 3
        self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
        self.timers = []
        self.key_cb = None
        self.mouse_cb = None
        self._tick = 0

    def fb_info(self):
        return (self.w, self.h, self.stride, self.bpp, 0)

    def fb_mem(self):
        return self.mem

    def fb_set_mode(self, w, h):
        return None

    def write(self, s):
        self.log.append(s)

    def after(self, ms, cb):
        self.timers.append((ms, cb))

    def tick(self):
        self._tick += 1
        return self._tick

    def on_key(self, cb):
        self.key_cb = cb

    def on_mouse(self, cb):
        self.mouse_cb = cb


kern = _Kern()
sys.modules['kern'] = kern
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra").resolve()))
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra/apps").resolve()))
import tk
import tk as tkmod
import editor
import mouse
from framebuffer import Framebuffer
from keyboard import Keyboard


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)

fb = Framebuffer()
kb = Keyboard()
ms = mouse.Mouse(fb)
root = tk.Tk(title="editor test")
root.start(fb, kb, ms)
quit_called = []
app = editor.Editor(root, on_quit=lambda: quit_called.append(1), path=None)

# initial focus is the editor (first focusable)
check(root._focused() is app.editor, "editor should start focused")

# typing inserts text
for c in "hello":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
check(app.editor.get_text() == "hello", app.editor.get_text())

# Enter splits the line; typing continues on the new line
kb._dispatch(("enter", 13, 1)); kb._dispatch(("enter", 13, 0))
for c in "world":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
check(app.editor.get_text() == "hello\nworld", app.editor.get_text())
check((app.editor.row, app.editor.col) == (1, 5),
      (app.editor.row, app.editor.col))

# arrow keys move the cursor
kb._dispatch(("up", 0, 1)); kb._dispatch(("up", 0, 0))
check(app.editor.row == 0, app.editor.row)
kb._dispatch(("left", 0, 1)); kb._dispatch(("left", 0, 0))
check(app.editor.col == 4, app.editor.col)

# backspace deletes the char before the cursor (at col 4 of "hello")
kb._dispatch(("backspace", 0, 1)); kb._dispatch(("backspace", 0, 0))
check(app.editor.get_text() == "helo\nworld", app.editor.get_text())
# at the start of a line, backspace joins it with the previous line
kb._dispatch(("down", 0, 1)); kb._dispatch(("down", 0, 0))
for _ in range(3):
    kb._dispatch(("left", 0, 1)); kb._dispatch(("left", 0, 0))
check((app.editor.row, app.editor.col) == (1, 0),
      (app.editor.row, app.editor.col))
kb._dispatch(("backspace", 0, 1)); kb._dispatch(("backspace", 0, 0))
check(app.editor.get_text() == "heloworld", app.editor.get_text())

# Delete key removes the char under the cursor
app.editor.load("abcd")
app.editor.row, app.editor.col = 0, 1
kb._dispatch(("del", 0, 1)); kb._dispatch(("del", 0, 0))
check(app.editor.get_text() == "acd", "delete mid failed: %r"
      % app.editor.get_text())
app.editor.load("abcd")
app.editor.row, app.editor.col = 0, 0
kb._dispatch(("del", 0, 1)); kb._dispatch(("del", 0, 0))
check(app.editor.get_text() == "bcd", "delete at start failed: %r"
      % app.editor.get_text())
app.editor.load("abcd")
app.editor.row, app.editor.col = 0, 3
kb._dispatch(("del", 0, 1)); kb._dispatch(("del", 0, 0))
check(app.editor.get_text() == "abc", "delete at end-of-line failed: %r"
      % app.editor.get_text())

# mouse click places the cursor
import text as textmod
app.editor.load("abcdefghij")
x = app.editor.x + app.editor.padx + 2 * textmod.char_width('a') + 4
y = app.editor.y + app.editor.pady + 1
app.editor.on_click(x, y, 1, 0)
check(app.editor.col >= 1 and app.editor.col <= 3, app.editor.col)

# F5 runs the buffer; print output lands in the console pane
app.editor.load("print(40 + 2)\nprint(\"hi\")")
kb._dispatch(("F5", 0, 1)); kb._dispatch(("F5", 0, 0))
texts = [s for s, c in app.pane.lines]
check(any("42" in s for s in texts), texts)
check(any("hi" in s for s in texts), texts)
check(any("=== run" in s for s in texts), texts)

# an exception in the buffer is reported in the pane
app.editor.load("raise ValueError(\"boom\")")
app.run_file()
texts = "\n".join(s for s, c in app.pane.lines)
check("ValueError" in texts and "boom" in texts, texts)

# the console pane evaluates lines with a persistent namespace
root._focus_idx = [w for w in root._focusables()].index(app.pane)
for c in "x=21":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
kb._dispatch(("enter", 13, 1)); kb._dispatch(("enter", 13, 0))
check(app.pane.ns.get("x") == 21, app.pane.ns.get("x"))
for c in "print(x)":
    kb._dispatch((c, ord(c), 1)); kb._dispatch((c, ord(c), 0))
kb._dispatch(("enter", 13, 1)); kb._dispatch(("enter", 13, 0))
texts = "\n".join(s for s, c in app.pane.lines)
check("21" in texts, texts)

# F6 toggles focus editor <-> pane
kb._dispatch(("F6", 0, 1)); kb._dispatch(("F6", 0, 0))
check(root._focused() is app.editor, root._focused())

# the Quit path (the '<Escape>' binding is installed by editor.run)
app.quit_app()
check(root.destroyed, "quit_app did not destroy the root")
check(quit_called == [1], quit_called)

# ---- the full run() path (root.start before build; keyboard alive) ----
quit2 = []
import editor as editormod
editormod.run(fb, kb, ms, on_quit=lambda: quit2.append(1), path=None)
root2 = tkmod._LAST_ROOT
check(root2 is not None and not root2.destroyed, "editor.run did not create a root")
kb2 = root2.kb
check(root2._focused() is root2.children[0], "editor should focus the EditorView")
for c in "hi":
    kb2._dispatch((c, ord(c), 1)); kb2._dispatch((c, ord(c), 0))
ev = root2.children[0]
check(ev.get_text() == "hi", "editor keyboard dead after run(): %r" % ev.get_text())
kb2._dispatch(("F5", 0, 1)); kb2._dispatch(("F5", 0, 0))
texts2 = "\n".join(s for s, c in ev.master.pane.lines) if hasattr(ev.master, "pane") else ""
kb2._dispatch(("esc", 0, 1)); kb2._dispatch(("esc", 0, 0))
check(quit2 == [1], "editor.run quit path broken: %r" % quit2)
check(root2.destroyed, "editor.run quit did not destroy the root")

print("EDITOR-TEST: all assertions passed")
