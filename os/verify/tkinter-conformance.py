# tkinter-conformance.py -- run the SAME sources the Ripos tkinter gate uses
# against the REAL stdlib tkinter, proving they are genuine, unmodified
# tkinter code.  Runs in its own process (no initramfs shim on sys.path).
#
# Usage:  py -3 os/target/tkinter-conformance.py

import sys
import tkinter as tk

# sanity: make sure we really got the stdlib package, not a stray shim
assert "initramfs" not in str(tk.__file__), tk.__file__


def hello():
    """The docs "Hello, Tkinter" example, verbatim."""
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

    root = tk.Tk()
    root.withdraw()
    app = Application(master=root)
    root.update_idletasks()
    assert app.hi_there["text"] == "Hello World\n(click me)"
    assert app.quit["text"] == "QUIT"
    assert app.quit.cget("fg") == "red"
    app.say_hi()
    root.destroy()
    print("conformance hello: OK")


def form():
    """The form demo API (grid/Entry/StringVar/bind), run against real Tk."""
    root = tk.Tk()
    root.title("Ripos form")

    name = tk.StringVar(value="Ripos")
    entry = tk.Entry(root, textvariable=name, width=24)
    entry.pack()
    root.update_idletasks()
    root.update()

    assert entry.get() == "Ripos"
    name.set("Ada")
    assert entry.get() == "Ada"
    entry.insert(0, "X")
    assert entry.get() == "XAda"
    entry.delete(0, 1)
    assert entry.get() == "Ada"
    entry.delete(0, "end")
    assert entry.get() == ""

    got = []
    def do_greet(event=None):
        got.append(entry.get())
    entry.bind("<Return>", do_greet)
    entry.focus_force()
    root.update()
    entry.event_generate("<Return>")
    root.update()
    assert got, "Return binding did not fire on real Tk"

    # note: real tkinter's cget('textvariable') returns the Tcl name string
    # ('PY_VAR0'), not the Python object; Ripos returns the object itself.
    msg = tk.StringVar(value="Press the button")
    lab = tk.Label(root, textvariable=msg)
    msg.set("Hello, Ada!")
    assert msg.get() == "Hello, Ada!"
    assert str(lab["textvariable"]) == str(msg)

    iv = tk.IntVar(value=3)
    iv.set("9")
    assert iv.get() == 9
    bv = tk.BooleanVar(value=False)
    bv.set(1)
    assert bv.get() is True
    dv = tk.DoubleVar(value=1.5)
    assert dv.get() == 1.5

    from tkinter import font
    f = font.Font(family="Arial", size=12)
    assert isinstance(f.measure("hello"), int) and f.measure("hello") > 0

    from tkinter import messagebox  # import-only; dialogs need a user
    assert messagebox.__name__ == "tkinter.messagebox"

    root.destroy()
    print("conformance form: OK")


hello()
form()
print("CONFORMANCE-OK")
