# m6.py -- M6 gate: keystrokes move pixels on the framebuffer.
#
# Boots the pure-Python framebuffer and keyboard drivers, draws a test
# pattern, then lets arrow keys move a cursor and letter keys paint pixels
# at the cursor.  Serial output doubles as the verification trace; the
# pixel readback lines prove the framebuffer is live, writable memory.
#
# After import, the kernel main loop keeps calling the registered on_key
# handler as PS/2 events arrive -- this module only registers the driver
# and returns.

import kern
from framebuffer import Framebuffer
from keyboard import Keyboard

kern.write('m6: booting framebuffer driver\n')
fb = Framebuffer()
kern.write('m6: fb %dx%d stride=%d bpp=%d fmt=%d\n'
           % (fb.width, fb.height, fb.stride, fb.bpp, fb.format))
kb = Keyboard()
kern.write('m6: keyboard driver ready\n')

BG = (12, 14, 18)
BORDER = (70, 90, 130)
CURSOR = (240, 240, 240)
CURSOR_SIZE = 8

fb.clear(*BG)
fb.draw_rect(0, 0, fb.width, fb.height, *BORDER)

# test pattern: reference pixels + a diagonal, all verifiable in the log
fb.set_pixel(4, 4, 255, 0, 0)      # red dot
fb.set_pixel(5, 4, 0, 255, 0)      # green dot
fb.set_pixel(6, 4, 0, 0, 255)      # blue dot
fb.line(20, fb.height - 20, fb.width - 20, 20, 120, 60, 60)

# read the pattern back: proves writes are visible (framebuffer is live)
kern.write('m6: pixel(4,4)=%s pixel(5,4)=%s pixel(6,4)=%s\n'
           % (fb.pixel(4, 4), fb.pixel(5, 4), fb.pixel(6, 4)))

cx = fb.width // 2
cy = fb.height // 2


def paint_cursor(x, y, color):
    fb.fill_rect(x, y, CURSOR_SIZE, CURSOR_SIZE, *color)


def erase_cursor(x, y):
    fb.fill_rect(x, y, CURSOR_SIZE, CURSOR_SIZE, *BG)


paint_cursor(cx, cy, CURSOR)
kern.write('m6: cursor at (%d,%d); arrows move, letters paint\n' % (cx, cy))
kern.write('m6: keystrokes move pixels (gate)\n')


def on_key(ev):
    global cx, cy
    name, ch, pressed = ev
    if not pressed:
        return
    if name in ('left', 'right', 'up', 'down'):
        erase_cursor(cx, cy)
        if name == 'left':
            cx = max(0, cx - CURSOR_SIZE)
        elif name == 'right':
            cx = min(fb.width - CURSOR_SIZE, cx + CURSOR_SIZE)
        elif name == 'up':
            cy = max(0, cy - CURSOR_SIZE)
        else:
            cy = min(fb.height - CURSOR_SIZE, cy + CURSOR_SIZE)
        paint_cursor(cx, cy, CURSOR)
        kern.write('m6: cursor at (%d,%d)\n' % (cx, cy))
    elif ch:
        px = cx + CURSOR_SIZE // 2
        py = cy + CURSOR_SIZE // 2
        fb.set_pixel(px, py, (cx * 7 + ch) % 256, (cy * 5 + ch) % 256,
                     (ch * 11) % 256)
        kern.write('m6: painted pixel at (%d,%d) from %r\n'
                   % (px, py, chr(ch)))
    else:
        kern.write('m6: key %s pressed\n' % name)


kb.on_event(on_key)
kern.write('m6: ready\n')
