# imgedit.py -- pixel paint editor for Ripos (M9.2).
#
# A full-screen paint app: a document (png.Image) rendered centered under a
# toolbar.  Left mouse button paints; keys choose the tool and color:
#   p  pencil    l  line    r  rect    e  eraser
#   1-6 palette colors      c  clear   s  save PNG
#   Esc quits back to the caller (shell / file manager).
# The document is a real png.Image, so 's' writes a valid PNG to the save
# path (default /home/edit.png on the writable RAM disk).  Ctrl+Z undo
# arrives with M9.6.

import kern
import png
import text
from keyboard import Keyboard
import tkinter as tk

TITLE_BG = (70, 95, 140)
TITLE_TEXT = (255, 255, 255)
TITLE_H = 16
BG = (16, 20, 28)
TOOL_BG = (26, 32, 42)
TOOL_H = 26
DOC_W, DOC_H = 256, 192

PALETTE = [
    (255, 255, 255), (0, 0, 0), (255, 0, 0),
    (0, 255, 0), (0, 0, 255), (255, 255, 0),
]
TOOL_KEYS = {'p': 'pencil', 'l': 'line', 'r': 'rect', 'e': 'eraser'}


def _line_points(x0, y0, x1, y1):
    """Bresenham line, inclusive endpoints."""
    pts = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        pts.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return pts


class Editor(tk.Tk):
    """Full-screen paint editor: a Tk root that renders a document canvas."""

    def __init__(self, caller_kb, on_quit):
        tk.Tk.__init__(self, title="imgedit", sidebar=True)
        self.caller_kb = caller_kb
        self.on_quit = on_quit
        self.bg = BG
        self.show_title = True
        self.doc = png.Image(DOC_W, DOC_H, "rgb")
        self.doc.pixels[:] = bytes([255, 255, 255]) * (DOC_W * DOC_H)
        self.tool = 'pencil'
        self.color = PALETTE[0]
        self.scale = 1
        self.ox = 0
        self.oy = 0
        self.save_path = "/home/edit.png"
        self._down = False
        self._dx = self._dy = 0      # doc-space start point of the stroke
        self._lx = self._ly = 0      # last drag point (doc space)

    def start(self, fb, kb, mouse=None):
        tk.Tk.start(self, fb, kb, mouse)
        self.scale = max(1, min(fb.width // DOC_W,
                                (fb.height - TITLE_H - TOOL_H - 8) // DOC_H))
        self.ox = (fb.width - DOC_W * self.scale) // 2
        self.oy = TITLE_H + TOOL_H + 4
        self.redraw()

    def _to_doc(self, x, y):
        return ((x - self.ox) // self.scale, (y - self.oy) // self.scale)

    def draw_region(self, fb, ox, oy, rw, rh):
        fb.fill_rect(ox, oy, rw, rh, *self.bg)
        if self.show_title:
            fb.fill_rect(ox, oy, rw, TITLE_H, *TITLE_BG)
            text.draw_text(fb, ox + 4, oy + 2, self._title, TITLE_TEXT)
        ty = TITLE_H
        fb.fill_rect(ox, ty, rw, TOOL_H, *TOOL_BG)
        head = "tool=%s  color:" % self.tool
        text.draw_text(fb, ox + 6, ty + 5, head, (220, 225, 235))
        sx = ox + 6 + text.text_width(head) + 10
        for i, c in enumerate(PALETTE):
            if c == self.color:
                fb.fill_rect(sx + i * 22 - 2, ty + 4, 18, 18, 235, 180, 40)
            fb.fill_rect(sx + i * 22, ty + 6, 14, 14, *c)
        # canvas (with border) then cursor
        self.doc.blit(fb, self.ox, self.oy, self.scale)
        fb.draw_rect(self.ox - 1, self.oy - 1,
                     DOC_W * self.scale + 2, DOC_H * self.scale + 2,
                     110, 120, 140)
        if self.mouse is not None:
            self.mouse.draw_cursor(fb)

    # ---- painting ---------------------------------------------------

    def _set(self, x, y, color=None):
        if 0 <= x < DOC_W and 0 <= y < DOC_H:
            self.doc.set(x, y, color if color is not None else self.color)

    def _stroke(self, x0, y0, x1, y1, erase=False):
        color = (255, 255, 255) if erase else self.color
        for (px, py) in _line_points(x0, y0, x1, y1):
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    qx, qy = px + dx, py + dy
                    if 0 <= qx < DOC_W and 0 <= qy < DOC_H:
                        self.doc.set(qx, qy, color)

    def on_mouse(self, ev):
        if self.destroyed:
            return
        x, y, button, pressed, dbl, clicks, wheel = ev
        if button == 1:
            dx, dy = self._to_doc(x, y)
            if pressed:
                self._down = True
                self._dx, self._dy = dx, dy
                self._lx, self._ly = dx, dy
                if self.tool in ('pencil', 'eraser'):
                    self._stroke(dx, dy, dx, dy, erase=(self.tool == 'eraser'))
            else:
                if self._down:
                    if self.tool == 'line':
                        self._line(self._dx, self._dy, dx, dy)
                    elif self.tool == 'rect':
                        self._rect(self._dx, self._dy, dx, dy)
                self._down = False
            self.redraw()
        elif button == 0 and self._down:
            # drag while the button is held (freehand)
            dx, dy = self._to_doc(x, y)
            if self.tool in ('pencil', 'eraser'):
                self._stroke(self._lx, self._ly, dx, dy,
                             erase=(self.tool == 'eraser'))
            self._lx, self._ly = dx, dy
            self.redraw()

    def _line(self, x0, y0, x1, y1):
        for (px, py) in _line_points(x0, y0, x1, y1):
            self._set(px, py)

    def _rect(self, x0, y0, x1, y1):
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        for x in range(x0, x1 + 1):
            self._set(x, y0)
            self._set(x, y1)
        for y in range(y0, y1 + 1):
            self._set(x0, y)
            self._set(x1, y)

    # ---- keys -------------------------------------------------------

    def on_key(self, ev):
        if self.destroyed:
            return
        name, ch, pressed = ev
        if not pressed:
            return
        if name == 'esc':
            self._quit()
            return
        if ch:
            c = chr(ch)
            if c in TOOL_KEYS:
                self.tool = TOOL_KEYS[c]
                self.redraw()
                return
            if c == 'c':
                self.doc.pixels[:] = bytes([255, 255, 255]) * (DOC_W * DOC_H)
                self.redraw()
                return
            if c in '123456':
                self.color = PALETTE[int(c) - 1]
                self.redraw()
                return
            if c in ('s', 'S'):
                self.save()
                return
        tk.Tk.on_key(self, ev)

    def save(self):
        try:
            data = png.save_png(self.doc)
            with open(self.save_path, "wb") as f:
                f.write(data)
            kern.write("imgedit: saved %s (%d bytes)\n" % (self.save_path, len(data)))
        except Exception as e:  # noqa: BLE001
            kern.write("imgedit: save failed: %r\n" % (e,))

    def _quit(self):
        if self.destroyed:
            return
        kern.write("imgedit: quit\n")
        try:
            self.caller_kb.activate()
        except AttributeError:
            kern.on_key(self.caller_kb._dispatch)
        self.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    """Start the paint editor.  kb is the CALLER keyboard (restored on
    quit); returns immediately (the kernel event loop drives the UI).
    path = where 's' saves the PNG (default /home/edit.png)."""
    kb2 = Keyboard()
    root = Editor(kb, on_quit)
    if path:
        root.save_path = path
    # app window system: title-bar '-'/'X' + left sidebar
    root._prev_kb = kb
    root._back = on_quit
    root._close_handler = root._quit
    root.start(fb, kb2, mouse)
    root.mainloop()
    kern.write("imgedit: editor ready (event-driven)\n")
