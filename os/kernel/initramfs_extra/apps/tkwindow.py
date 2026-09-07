# tkwindow.py -- tk widget tree embedded inside a wm window.
#
# Proves the wm integration: the widget tree lives in a wm.Window (via the
# draw_content hook) alongside the normal window-manager chrome.  Enter
# presses the focused button inside the window.

import kern
from framebuffer import Framebuffer
from keyboard import Keyboard
import wm
import tk

kern.write('tkwindow: booting wm + embedded widget window\n')

fb = Framebuffer()
kb = Keyboard()

root = tk.Tk(title='widgets')

tk.Label(root, text='tk tree inside wm.Window', fg=(150, 200, 150)).pack()
tk.Button(root, text='Press me',
          command=lambda: kern.write('tkwindow: embedded button clicked\n')).pack()
cv = tk.Canvas(root, width=200, height=60)
cv.create_line(5, 50, 190, 10, color=(255, 160, 80))
cv.create_text(20, 20, 'canvas', color=(255, 255, 255))
cv.pack()

win = tk.WidgetWindow('win.widgets', int(fb.width * 0.05), int(fb.height * 0.10),
                      500, 170, root)
desk = wm.Desktop(fb, kb, [win], focus=0, lang='en')

kern.write('tkwindow: widget window in desktop (Enter to click)\n')
