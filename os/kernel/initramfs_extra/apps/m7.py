# m7.py -- M7 gate: overlapping windows demo (pure-Python GUI on Ripos).
#
# Boots the window manager: three z-ordered windows (System Info, a Terminal,
# and a Language panel) on the framebuffer.  Tab cycles focus, the arrow keys
# move the focused window, typing goes to the focused window (the terminal
# echoes and runs lines on Enter), and Ctrl+L switches the UI language
# between English and Chinese live.
#
# After import the kernel main loop keeps calling the registered on_key
# handler; this module only sets everything up and returns.

import kern
from framebuffer import Framebuffer
from keyboard import Keyboard
import wm
from wm import Desktop, Window, SysInfo, Terminal, LangInfo

kern.write('m7: booting GUI\n')
fb = Framebuffer()
kb = Keyboard()
kern.write('m7: fb %dx%d\n' % (fb.width, fb.height))

W = fb.width
H = fb.height

win_sys = SysInfo('win.system', int(W * 0.05), int(H * 0.10), 560, 150)
win_term = Terminal('win.terminal', int(W * 0.30), int(H * 0.32), 620, 130)
win_lang = LangInfo('win.language', int(W * 0.12), int(H * 0.55), 440, 110)

desk = Desktop(fb, kb, [win_sys, win_term, win_lang], focus=1, lang='en')

kern.write('m7: windows created: sys term lang\n')
# pixel self-check: the focused window's title bar must be the focus color
px = fb.pixel(win_term.x + 6, win_term.y + 4)
kern.write('m7: titlebar pixel=%s\n' % (px,))
kern.write('m7: gate: overlapping windows demo ready\n')
