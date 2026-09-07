# tkform.py -- a small form written against the REAL tkinter API, running
# on the Ripos framebuffer.  Boot it from the M8 shell with:  import tkform
#
# Exercises the parts of tkinter that Ripos implements: Tk().title(),
# grid() with sticky/columnspan, Entry + StringVar, bind('<Return>'),
# Button command callbacks and Label textvariable.  Interaction:
#   Tab          cycle focus (entry <-> button)
#   type + Enter submit the name (or press the Greet button)

import tkinter as tk
import kern


def greet():
    name = entry.get()
    msg.set("Hello, %s!" % name)
    kern.write('tkform: greeting "%s"\n' % msg.get())


root = tk.Tk()
root.title("Ripos form")

name = tk.StringVar(value="Ripos")

tk.Label(root, text="Your name:").grid(row=0, column=0, sticky="w")
entry = tk.Entry(root, textvariable=name, width=24)
entry.grid(row=0, column=1)
entry.bind("<Return>", lambda e: greet())

msg = tk.StringVar(value="Press the button")
tk.Label(root, textvariable=msg).grid(row=1, column=0, columnspan=2)

btn = tk.Button(root, text="Greet", command=greet)
btn.grid(row=2, column=0, columnspan=2)

kern.write('tkform: grid form ready (type + Enter, or Tab/Enter to press Greet)\n')
root.mainloop()
