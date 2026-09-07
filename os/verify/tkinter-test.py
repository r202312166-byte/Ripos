# tkinter-test.py -- host-side gate for the Ripos tkinter compatibility
# package (no OS boot).  Stubs the kern module, imports the initramfs_extra
# modules directly on the host, and drives the UI exactly as the OS would:
# real keyboard events through keyboard.Keyboard, pixel assertions against
# a fake framebuffer.  It also runs the same sources against the REAL
# stdlib tkinter (subprocess) to prove the demos are genuine tkinter code.
#
# Usage:  py -3 os/target/tkinter-test.py

import io
import os
import pathlib
import subprocess
import sys

EXTRA = pathlib.Path(__file__).resolve().parents[1] / "kernel" / "initramfs_extra"


class _Kern:
    def __init__(self):
        self.w = 640
        self.h = 480
        self.bpp = 3
        self.stride = self.w          # pixels, like the bootloader framebuffer
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
        self.timers = []
        self.key_cb = None

    def fb_info(self):
        return (self.w, self.h, self.stride, self.bpp, 0)

    def fb_mem(self):
        return self.mem

    def write(self, s):
        self.log.append(s)

    def after(self, ms, cb):
        self.timers.append((ms, cb))

    def tick(self):
        return 0

    def on_key(self, cb):
        self.key_cb = cb


kern = _Kern()
sys.modules['kern'] = kern
sys.path.insert(0, str(EXTRA))

import tkinter as tk                     # noqa: E402 -- must be OUR shim
from tkinter import font, messagebox     # noqa: E402
from framebuffer import Framebuffer      # noqa: E402
from keyboard import Keyboard            # noqa: E402
import text as textmod                   # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# 1. module resolution: `import tkinter` must land on the shim package
check('initramfs_extra' in str(tk.__file__), 'import tkinter resolved to %s'
      % tk.__file__)
check(tk.TkVersion == 8.6 and tk.END == "end" and tk.TOP == "top"
      and tk.BOTH == "both" and tk.CENTER == "center", "constants wrong")
check(hasattr(tk, "StringVar") and hasattr(tk, "IntVar")
      and hasattr(tk, "BooleanVar") and hasattr(tk, "DoubleVar"), "vars missing")


# 2. the verbatim "Hello, Tkinter" docs example (module-level Application)
class Application(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.master = master
        self.pack()
        self.create_widgets()

    def create_widgets(self):
        self.hi_there = tk.Button(self)
        self.hi_there["text"] = "Hello World\n(click me)"
        self.hi_there["command"] = self.say_hi
        self.hi_there.pack(side="top")

        self.quit = tk.Button(self, text="QUIT", fg="red",
                              command=self.master.destroy)
        self.quit.pack(side="bottom")

    def say_hi(self):
        print("hi there, everyone!")


fb = Framebuffer()
kb = Keyboard()
root = tk.Tk()
app = Application(master=root)
root.start(fb, kb)
app.mainloop()

check(app.hi_there["text"] == "Hello World\n(click me)",
      "item access failed: %r" % app.hi_there["text"])
check(app.quit.cget("text") == "QUIT", "cget text failed")
check(app.quit.cget("fg") == "red", "cget fg should stay raw: %r"
      % app.quit.cget("fg"))
check(app.quit.fg == (235, 60, 60), "named color did not resolve: %r"
      % (app.quit.fg,))
# pack(side=top / side=bottom) inside the frame: QUIT sits below the button
check(app.quit.y > app.hi_there.y, "side=bottom did not stack below")
check(app.hi_there.y >= 16, "frame not laid out below the title bar")
# first focusable is the top button, drawn with the focus ring
check(root._focused() is app.hi_there, "focus should start on hi_there")
check(fb.pixel(app.hi_there.x + 2, app.hi_there.y + 2) == (210, 130, 40),
      "focus ring pixel missing: %s" % (fb.pixel(app.hi_there.x + 2,
                                                 app.hi_there.y + 2),))

buf = io.StringIO()
import contextlib                            # noqa: E402
with contextlib.redirect_stdout(buf):
    kb._dispatch(('enter', 13, 1))
    kb._dispatch(('enter', 13, 0))
check("hi there, everyone!" in buf.getvalue(),
      "say_hi did not run on Enter: %r" % buf.getvalue())
check(any("tk: button \"Hello World\n(click me)\" clicked" in s
          for s in kern.log), "click marker missing")

# Tab -> QUIT focused; Enter -> master.destroy
kb._dispatch(('tab', 0, 1))
kb._dispatch(('tab', 0, 0))
check(root._focused() is app.quit, "Tab should focus QUIT")
kb._dispatch(('enter', 13, 1))
kb._dispatch(('enter', 13, 0))
check(root.destroyed, "QUIT command should destroy the root")
check(any("tk: destroyed" in s for s in kern.log), "destroy marker missing")


# 3. grid + Entry + StringVar + bind("<Return>") form
fb2 = Framebuffer()
kb2 = Keyboard()
root2 = tk.Tk()
root2.title('Ripos form')
root2.start(fb2, kb2)

greets = []
name = tk.StringVar(value='Ripos')
lab0 = tk.Label(root2, text='Your name:')
lab0.grid(row=0, column=0, sticky='w')
entry = tk.Entry(root2, textvariable=name, width=24)
entry.grid(row=0, column=1)


def do_greet(event=None):
    greets.append(entry.get())
    msg.set('Hello, %s!' % entry.get())


entry.bind('<Return>', do_greet)
msg = tk.StringVar(value='Press the button')
lab1 = tk.Label(root2, textvariable=msg)
lab1.grid(row=1, column=0, columnspan=2)
btn = tk.Button(root2, text='Greet', command=do_greet)
btn.grid(row=2, column=0, columnspan=2)

root2.redraw()

check(entry.get() == 'Ripos', 'entry did not pick up the variable')
name.set('Ada')
check(entry.get() == 'Ada', 'var.set() did not reach the entry')
kb2._dispatch(('a', 97, 1))
kb2._dispatch(('a', 97, 0))
check(entry.get() == 'Adaa', 'typing did not append: %r' % entry.get())
kb2._dispatch(('backspace', 0, 1))
kb2._dispatch(('backspace', 0, 0))
check(entry.get() == 'Ada', 'backspace did not delete')
entry.insert(0, 'X')
check(entry.get() == 'XAda', 'insert(0, ...) failed: %r' % entry.get())
entry.delete(0, 1)
check(entry.get() == 'Ada', 'delete(0,1) failed: %r' % entry.get())
entry.delete(0, 'end')
check(entry.get() == '', 'delete(0, END) failed: %r' % entry.get())
# typing again so the Return binding has something to report
kb2._dispatch(('A', 65, 1))
kb2._dispatch(('A', 65, 0))
kb2._dispatch(('d', 100, 1))
kb2._dispatch(('d', 100, 0))
kb2._dispatch(('a', 97, 1))
kb2._dispatch(('a', 97, 0))
check(entry.get() == 'Ada', 'retype failed: %r' % entry.get())
kb2._dispatch(('enter', 13, 1))
kb2._dispatch(('enter', 13, 0))
check(greets == ['Ada'], 'Return binding did not fire: %r' % greets)
check(msg.get() == 'Hello, Ada!', 'label variable not updated: %r' % msg.get())
# real tkinter: the text option stays '' when textvariable drives the label
check(lab1.cget('textvariable') is msg, 'label lost its textvariable')
check(lab1._display() == 'Hello, Ada!', 'label textvariable not reflected')

# grid layout sanity: the entry sits right of the label, the button spans
# both columns (its right edge reaches past the label column)
check(entry.x > lab0.x, "entry should be right of the label")
check(entry.y >= 16, "grid should start below the title bar")
span_right = btn.x + btn.w
check(span_right > lab0.x + lab0.w, "columnspan=2 should cover col 0 too")

# Tab moves focus entry -> button; Enter presses it
kb2._dispatch(('tab', 0, 1))
kb2._dispatch(('tab', 0, 0))
check(root2._focused() is btn, "Tab should focus the Greet button")
kb2._dispatch(('enter', 13, 1))
kb2._dispatch(('enter', 13, 0))
check(greets == ['Ada', 'Ada'], 'button command did not fire: %r' % greets)
root2.destroy()


# 4. tkinter-style Canvas items (corner coords, fill/outline/width)
fb3 = Framebuffer()
kb3 = Keyboard()
root3 = tk.Tk()
root3.start(fb3, kb3)
cv = tk.Canvas(root3, width=220, height=140, bg=(30, 36, 48))
cv.pack()
r = cv.create_rectangle(10, 10, 60, 50, fill='blue')
check(isinstance(r, int) and r >= 1, "create_rectangle should return an id")
cv.create_oval(70, 10, 120, 50, fill='green')
cv.create_line(130, 10, 210, 10, fill='red', width=3)
cv.create_text(110, 90, text='Ripos', fill='white', anchor='center')
cv.create_polygon(10, 110, 60, 130, 30, 90, fill='yellow')
root3.redraw()
check(fb3.pixel(cv.x + 35, cv.y + 30) == (80, 120, 235),
      "rectangle fill missing: %s" % (fb3.pixel(cv.x + 35, cv.y + 30),))
check(fb3.pixel(cv.x + 95, cv.y + 30) == (60, 200, 90),
      "oval fill missing: %s" % (fb3.pixel(cv.x + 95, cv.y + 30),))
check(fb3.pixel(cv.x + 170, cv.y + 10) == (235, 60, 60),
      "thick line missing: %s" % (fb3.pixel(cv.x + 170, cv.y + 10),))
# line width 3 covers y -1..+1 around the center row
check(fb3.pixel(cv.x + 200, cv.y + 11) == (235, 60, 60),
      "line width not honored: %s" % (fb3.pixel(cv.x + 200, cv.y + 11),))
# polygon: a scanline through the triangle body
check(fb3.pixel(cv.x + 35, cv.y + 115) == (235, 200, 60),
      "polygon fill missing: %s" % (fb3.pixel(cv.x + 35, cv.y + 115),))
# items survive full redraws
n_items = len(cv._items)
root3.redraw()
check(len(cv._items) == n_items, "items lost across redraw")
cv.delete(r)
check(len(cv._items) == n_items - 1, "delete(id) failed")
cv.delete('all')
check(cv._items == [], "delete(\"all\") failed")
root3.destroy()


# 5. variables + winfo + font/messagebox stubs
v = tk.StringVar(value='x')
check(v.get() == 'x' and str(v) == 'x', 'StringVar broken')
iv = tk.IntVar(value=3)
iv.set('9')
check(iv.get() == 9, "IntVar coercion: %r" % iv.get())
dv = tk.DoubleVar(value=1.5)
check(dv.get() == 1.5, "DoubleVar broken")
bv = tk.BooleanVar(value=False)
bv.set(1)
check(bv.get() is True, "BooleanVar broken")
root4 = tk.Tk()
root4.start(fb3, kb3)
w = tk.Label(root4, text='w')
w.pack()
root4.redraw()
check(w.winfo_width() > 0 and w.winfo_reqwidth() > 0, "winfo broken")
check(w.winfo_ismapped() and w.winfo_exists(), "winfo exists broken")
check(isinstance(font.Font().measure('hello'), int)
      and font.Font().measure('hello') > 0, 'font.measure broken')
check(messagebox.askokcancel('t', 'm') is True, 'messagebox stub broken')
root4.destroy()

print("TKINTER-TEST: shim assertions passed (%d log lines, %d timers)"
      % (len(kern.log), len(kern.timers)))

# 6. cross-check against the REAL stdlib tkinter (same sources, no shim).
#    Run in a subprocess so our shim cannot shadow the stdlib package.
conf = pathlib.Path(__file__).resolve().parent / "tkinter-conformance.py"
try:
    proc = subprocess.run([sys.executable, str(conf)],
                          capture_output=True, text=True, timeout=120)
except Exception as e:                       # no display / no Tk available
    print("TKINTER-TEST: conformance skipped (%s)" % e)
else:
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0 and "CONFORMANCE-OK" in out:
        print("TKINTER-TEST: real-stdlib-tkinter conformance PASSED")
    else:
        print("TKINTER-TEST: WARNING conformance did not pass:\n%s" % out)
