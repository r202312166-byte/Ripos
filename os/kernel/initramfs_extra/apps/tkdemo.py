# tkdemo.py -- tkinter-style widget demo on Ripos (pure Python, no Tcl/Tk).
#
# Standalone demo: a full-screen widget GUI driven entirely by the keyboard.
#   Tab        cycle focus between buttons (orange = focused)
#   Enter      press the focused button
#
# Boot it from the M8 shell with:  import tkdemo

import kern
import tk

kern.write('tkdemo: booting widget demo\n')

root = tk.Tk(title='tk: Ripos widget demo')

clicks = [0]
status = tk.Label(root, text='tkinter-style widgets, no Tcl/Tk', fg=(120, 200, 120))
status.pack()


def bump():
    clicks[0] += 1
    status.set_text('clicks: %d   (Tab + Enter to press)' % clicks[0])


btn = tk.Button(root, text='Click me', command=bump)
btn.pack()

cv = tk.Canvas(root, width=300, height=90)
cv.create_rect(4, 4, 150, 82, color=(40, 60, 90))
cv.create_line(10, 70, 140, 10, color=(235, 90, 90))
cv.create_text(22, 40, 'hello from Canvas', color=(255, 255, 255))
cv.create_pixel(5, 5, color=(255, 255, 0))
cv.pack()

quit_btn = tk.Button(root, text='Quit', command=root.destroy)
quit_btn.pack()

kern.write('tkdemo: 4 widgets (label, button, canvas, quit)\n')

# Non-blocking: hands the keyboard + a 1s redraw timer to the kernel loop.
root.mainloop()
kern.write('tkdemo: mainloop returned (event-driven; kernel drives)\n')
