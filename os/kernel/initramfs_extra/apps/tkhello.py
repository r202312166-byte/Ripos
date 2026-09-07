# tkhello.py -- the canonical "Hello, Tkinter" example from the Python docs,
# UNMODIFIED, running on the Ripos framebuffer through the tkinter compat
# package.  Boot it from the M8 shell with:  import tkhello
#
# The only differences from the docs are this comment.  The program uses
# tk.Frame as a base class, item access (widget["text"] = ...), pack(side=),
# named colors (fg="red") and command= callbacks -- all real tkinter API.
# print() goes to the serial console (stdout), like the M8 shell.

import tkinter as tk


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
app = Application(master=root)
app.mainloop()
