"""tk.py -- a tkinter-compatible widget toolkit for Ripos (pure Python).

A small, event-driven widget layer on top of the framebuffer driver and the
wm window manager.  There is no Tcl/Tk and no X server: widgets draw through
framebuffer.Framebuffer + text, and the PS/2 keyboard is the only input
device.

The API deliberately mirrors tkinter's so that real tkinter programs run
almost unmodified -- see the companion `tkinter` package (initramfs_extra/
tkinter/), which re-exports these widgets under the standard module name,
adds the tkinter constants and the module-level helpers (mainloop, font,
messagebox), and is what `import tkinter` resolves to on Ripos.

Geometry managers: pack() (side/expand/fill), grid() (row/column/span/
sticky) and place(x, y).  Widgets: Label, Button, Entry, Canvas, Frame,
plus the variables StringVar/IntVar/DoubleVar/BooleanVar.  Widget options
are readable/writable through configure()/cget() and item access
(widget['text'] = '...').

mainloop() hands control to the kernel event loop and returns immediately
(the kernel keeps dispatching keys and timers), so it must be the last
statement of the script -- exactly like importing m7 or the M8 shell.  An
infinite loop here would starve the cooperative kernel (preemption is off).

Keyboard-only: Tab cycles focus, Enter/Space activates the focused widget,
printable characters go to the focused Entry/Label, Backspace edits the
focused Entry.  There is no mouse.
"""

import kern
import text as textmod   # module alias so widget text= params don't shadow it
import wm

# ---------------------------------------------------------------------------
# colors

_NAMED_COLORS = {
    'black': (0, 0, 0), 'white': (255, 255, 255),
    'red': (235, 60, 60), 'green': (60, 200, 90), 'blue': (80, 120, 235),
    'yellow': (235, 200, 60), 'cyan': (80, 200, 220), 'magenta': (220, 90, 200),
    'orange': (235, 140, 50), 'purple': (160, 90, 210), 'pink': (235, 140, 170),
    'gray': (140, 140, 140), 'grey': (140, 140, 140),
    'darkgray': (90, 90, 90), 'darkgrey': (90, 90, 90),
    'lightgray': (200, 200, 200), 'lightgrey': (200, 200, 200),
    'brown': (150, 90, 50), 'navy': (40, 60, 120),
    'gold': (220, 180, 60), 'silver': (200, 200, 205), 'lime': (120, 230, 80),
    'teal': (50, 150, 150), 'indigo': (110, 70, 190), 'violet': (190, 120, 220),
    'coral': (240, 120, 100), 'salmon': (235, 140, 120), 'khaki': (200, 190, 120),
    'olive': (120, 130, 50), 'maroon': (130, 40, 40), 'beige': (210, 200, 170),
}


def _color(c):
    """Accept (r, g, b) tuples, '#rrggbb' strings or X11-style names."""
    if isinstance(c, (tuple, list)):
        return tuple(int(v) for v in c)
    if isinstance(c, str):
        s = c.strip().lower()
        if s.startswith('#') and len(s) == 7:
            try:
                return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
            except ValueError:
                pass
        if s in _NAMED_COLORS:
            return _NAMED_COLORS[s]
    return (200, 200, 200)   # permissive fallback for unknown colors


# ---------------------------------------------------------------------------
# variables (StringVar / IntVar / DoubleVar / BooleanVar)

class _Var:
    """Base variable: set()/get() plus widgets that redraw on change."""

    def __init__(self, master=None, value=None, name=None):
        self._master = master
        self._name = name
        self._value = self._default() if value is None else self._coerce(value)
        self._widgets = []

    def _default(self):
        return None

    def _coerce(self, v):
        return v

    def get(self):
        return self._value

    def set(self, v):
        self._value = self._coerce(v)
        for w in list(self._widgets):
            if not getattr(w, 'destroyed', False):
                w._var_changed()

    def _attach(self, w):
        if w not in self._widgets:
            self._widgets.append(w)

    def __str__(self):
        return str(self._value)

    __repr__ = __str__


class StringVar(_Var):
    def _default(self):
        return ''

    def _coerce(self, v):
        return str(v)


class IntVar(_Var):
    def _default(self):
        return 0

    def _coerce(self, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0


class DoubleVar(_Var):
    def _default(self):
        return 0.0

    def _coerce(self, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0


class BooleanVar(_Var):
    def _default(self):
        return False

    def _coerce(self, v):
        return bool(v)


# ---------------------------------------------------------------------------
# layout helpers

# Pack call sequence: tkinter lays out in the order pack() was called, not
# the order widgets were created.  Each pack() call stamps a fresh sequence
# number and _pack_layout sorts by it.
_PACK_SEQ = [0]


def _next_pack_seq():
    _PACK_SEQ[0] += 1
    return _PACK_SEQ[0]


def _pad_pair(v):
    """tkinter padx/pady may be an int or a (a, b) tuple."""
    if isinstance(v, (tuple, list)):
        return int(v[0]), int(v[1])
    return int(v), int(v)


def _nat(w, axis):
    """Natural (requested) size of a widget along one axis."""
    if isinstance(w, Frame):
        nw, nh = w._natural()
        return nw if axis == 'w' else nh
    v = getattr(w, '_natw', None) if axis == 'w' else getattr(w, '_nath', None)
    if v is None:
        v = w.w if axis == 'w' else w.h
    return v


def _pack_layout(container, cavity, children):
    """Pack children into the cavity (x, y, w, h) in pack order.

    Returns {widget: (x, y, w, h)} with absolute coordinates.  side='top'
    is the default and reproduces the classic vertical stack; 'bottom',
    'left' and 'right' follow tkinter's parcel model; expand stretches the
    leftover cavity across expand= widgets; fill stretches within a parcel.
    """
    order = [w for w in children if w._pack is not None]
    order.sort(key=lambda w: w._pack['_seq'])
    if not order:
        return {}
    cx, cy, cw, ch = cavity
    placements = {}
    # pass 1: fixed bands, in pack order
    for w in order:
        o = w._pack
        side = o['side']
        fill = o['fill']
        anchor = o['anchor']
        px0, px1 = _pad_pair(o['padx'])
        py0, py1 = _pad_pair(o['pady'])
        nw = _nat(w, 'w')
        nh = _nat(w, 'h')
        if side in ('top', 'bottom'):
            aw = nw
            if fill in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            ax = cx + px0
            if anchor == 'center':
                ax = cx + (cw - px0 - px1 - aw) // 2 + px0
            elif anchor == 'e':
                ax = cx + cw - px0 - aw
            ay = cy + py0
            ah = nh
            if fill in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            placements[w] = (ax, ay, aw, ah)
            cy += nh + py0 + py1
            ch -= nh + py0 + py1
        else:  # left / right
            ah = nh
            if fill in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            ay = cy + py0
            if anchor == 'center':
                ay = cy + (ch - py0 - py1 - ah) // 2 + py0
            elif anchor == 's':
                ay = cy + ch - py0 - ah
            ax = cx + px0
            aw = nw
            if fill in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            placements[w] = (ax, ay, aw, ah)
            cx += nw + px0 + px1
            cw -= nw + px0 + px1
    # pass 2: expand= widgets grow into the leftover cavity
    if cw > 0 or ch > 0:
        vexp = [w for w in order
                if w._pack.get('expand') and w._pack['side'] in ('top', 'bottom')]
        hexp = [w for w in order
                if w._pack.get('expand') and w._pack['side'] in ('left', 'right')]
        if vexp and ch > 0:
            base = cy
            each = ch // len(vexp)
            rem = ch % len(vexp)
            cur = base
            for i, w in enumerate(vexp):
                x, y, ww, hh = placements[w]
                end = cur + each + (1 if i < rem else 0)
                o = w._pack
                py0, py1 = _pad_pair(o['pady'])
                if o['fill'] in ('y', 'both'):
                    placements[w] = (x, y, ww, max(0, end - y - py0 - py1))
                else:
                    placements[w] = (x, y, ww, hh)
                cur = end
        if hexp and cw > 0:
            base = cx
            each = cw // len(hexp)
            rem = cw % len(hexp)
            cur = base
            for i, w in enumerate(hexp):
                x, y, ww, hh = placements[w]
                end = cur + each + (1 if i < rem else 0)
                o = w._pack
                px0, px1 = _pad_pair(o['padx'])
                if o['fill'] in ('x', 'both'):
                    placements[w] = (x, y, max(0, end - x - px0 - px1), hh)
                else:
                    placements[w] = (x, y, ww, hh)
                cur = end
    return placements


def _grid_layout(container, cavity, children):
    """Grid geometry: row/column cells, optional rowspan/columnspan/sticky."""
    gridded = [w for w in children if w._grid is not None]
    if not gridded:
        return {}
    cx, cy, cw, ch = cavity
    rows = {}
    cols = {}
    for w in gridded:
        g = w._grid
        rows.setdefault(g['row'], 0)
        cols.setdefault(g['column'], 0)
    row_h = {r: 0 for r in rows}
    col_w = {c: 0 for c in cols}
    for w in gridded:
        g = w._grid
        px0, px1 = _pad_pair(g.get('padx', 0))
        py0, py1 = _pad_pair(g.get('pady', 0))
        row_h[g['row']] = max(row_h[g['row']], _nat(w, 'h') + py0 + py1)
        col_w[g['column']] = max(col_w[g['column']], _nat(w, 'w') + px0 + px1)
    rows_sorted = sorted(rows)
    cols_sorted = sorted(cols)
    cell_x = {}
    x = cx
    for c in cols_sorted:
        cell_x[c] = x
        x += col_w[c]
    cell_y = {}
    y = cy
    for r in rows_sorted:
        cell_y[r] = y
        y += row_h[r]
    placements = {}
    for w in gridded:
        g = w._grid
        r, c = g['row'], g['column']
        rs = max(int(g.get('rowspan', 1)), 1)
        cs = max(int(g.get('columnspan', 1)), 1)
        i = rows_sorted.index(r)
        j = cols_sorted.index(c)
        span_w = sum(col_w[cc] for cc in cols_sorted[j:j + cs])
        span_h = sum(row_h[rr] for rr in rows_sorted[i:i + rs])
        px0, px1 = _pad_pair(g.get('padx', 0))
        py0, py1 = _pad_pair(g.get('pady', 0))
        sticky = g.get('sticky', '')
        aw = _nat(w, 'w')
        ah = _nat(w, 'h')
        if 'w' in sticky or 'e' in sticky:
            aw = max(0, span_w - px0 - px1)
        if 'n' in sticky or 's' in sticky:
            ah = max(0, span_h - py0 - py1)
        cellx = cell_x[c] + px0
        celly = cell_y[r] + py0
        room_w = span_w - px0 - px1
        room_h = span_h - py0 - py1
        if 'w' in sticky:
            wx = cellx
        elif 'e' in sticky:
            wx = cellx + room_w - aw
        else:
            wx = cellx + (room_w - aw) // 2
        if 'n' in sticky:
            wy = celly
        elif 's' in sticky:
            wy = celly + room_h - ah
        else:
            wy = celly + (room_h - ah) // 2
        placements[w] = (wx, wy, aw, ah)
    return placements


def _natural_size(children):
    """Bounding box of a container's children under their geometry managers."""
    w = h = 0
    packed = [c for c in children if c._pack is not None]
    tops = [c for c in packed if c._pack['side'] in ('top', 'bottom')]
    sides = [c for c in packed if c._pack['side'] in ('left', 'right')]
    def _pad(c, axis):
        o = c._pack
        return _pad_pair(o.get(axis, getattr(c, axis, 0)))

    if tops:
        w = max(w, max(_nat(c, 'w') for c in tops))
        h += sum(_nat(c, 'h') + sum(_pad(c, 'pady')) for c in tops)
    if sides:
        w += sum(_nat(c, 'w') + sum(_pad(c, 'padx')) for c in sides)
        h = max(h, max(_nat(c, 'h') + sum(_pad(c, 'pady')) for c in sides))
    gridded = [c for c in children if c._grid is not None]
    if gridded:
        rows = {}
        cols = {}
        for c in gridded:
            g = c._grid
            rows.setdefault(g['row'], 0)
            cols.setdefault(g['column'], 0)
        rh = {r: 0 for r in rows}
        cw = {c: 0 for c in cols}
        for c in gridded:
            g = c._grid
            px0, px1 = _pad_pair(g.get('padx', 0))
            py0, py1 = _pad_pair(g.get('pady', 0))
            rh[g['row']] = max(rh[g['row']], _nat(c, 'h') + py0 + py1)
            cw[g['column']] = max(cw[g['column']], _nat(c, 'w') + px0 + px1)
        w = max(w, sum(cw.values()))
        h = max(h, sum(rh.values()))
    for c in children:
        if c._abs is not None:
            w = max(w, c._abs[0] + _nat(c, 'w'))
            h = max(h, c._abs[1] + _nat(c, 'h'))
    return max(w, 1), max(h, 1)


def _layout(container, cavity, place_origin, children):
    """Place all children (pack + grid + place).  cavity is the pack/grid
    region (x, y, w, h); place_origin is the (ox, oy) that place() offsets
    are relative to (the root origin, not below the title bar).

    Returns {widget: (x, y, w, h)} in absolute coordinates.
    """
    placements = {}
    placements.update(_pack_layout(container, cavity, children))
    placements.update(_grid_layout(container, cavity, children))
    ox, oy = place_origin
    for w in children:
        if w._abs is not None:
            placements[w] = (ox + w._abs[0], oy + w._abs[1], _nat(w, 'w'),
                             _nat(w, 'h'))
    return placements


# ---------------------------------------------------------------------------
# widgets

class Widget:
    """Base widget: knows its master (Tk or Frame), its geometry and options.

    Options are readable/writable via widget['name'] / widget.cget(name) /
    widget.configure(**kw); unknown options are accepted and stored without
    error (Ripos is permissive where tkinter raises TclError).
    """

    focusable = False

    def __init__(self, master, padx=8, pady=3, **kw):
        self.master = master
        self.padx = padx
        self.pady = pady
        self.x = 0            # last drawn position (absolute)
        self.y = 0
        self.w = 0
        self.h = 0
        self._natw = 0
        self._nath = 0
        self.focused = False
        self._abs = None      # place() position; None = pack/grid
        self._pack = None     # dict of pack options; None = not packed
        self._grid = None     # dict of grid options; None = not gridded
        self._binds = {}      # event sequence -> [callbacks]
        self._opts = {}       # raw option storage (for cget/configure)
        self.destroyed = False
        master.add(self)

    # ---- option access ------------------------------------------------

    def configure(self, **kw):
        for k, v in kw.items():
            if k in ('fg', 'bg', 'focus', 'insertbackground', 'activebackground',
                     'highlightbackground', 'highlightcolor'):
                if k in ('fg', 'bg', 'focus', 'insertbackground'):
                    setattr(self, k, _color(v))
                self._opts[k] = v
            elif k in ('text', 'command', 'textvariable', 'width', 'height',
                       'show', 'padx', 'pady', 'relief', 'bd', 'font', 'anchor',
                       'justify', 'wraplength', 'state'):
                self._opts[k] = v
                if k in ('text', 'command', 'textvariable', 'width', 'height',
                         'show', 'padx', 'pady'):
                    if hasattr(self, k):
                        setattr(self, k, v)
            else:
                self._opts[k] = v
        self._recalc()
        self._root().redraw()

    config = configure

    def cget(self, name):
        if name in self._opts:
            return self._opts[name]
        return getattr(self, name, None)

    def __getitem__(self, name):
        return self.cget(name)

    def __setitem__(self, name, value):
        self.configure(**{name: value})

    def _recalc(self):
        pass

    def _var_changed(self):
        self._recalc()
        self._root().redraw()

    # ---- geometry managers --------------------------------------------

    def pack(self, **opts):
        """Pack into the master (tkinter pack semantics).  side=top is the
        default; expand stretches into leftover space; fill stretches the
        widget inside its parcel ('x'/'y'/'both')."""
        self._abs = None
        self._grid = None
        self._pack = {
            'side': opts.get('side', 'top'),
            'fill': opts.get('fill', 'none'),
            'expand': bool(opts.get('expand', False)),
            'anchor': opts.get('anchor', 'w'),
            # legacy Ripos pack() had no options and used the widget's own
            # padx/pady; keep that as the default so existing layouts are
            # unchanged (tkinter's default is 0 -- pass padx=0 explicitly)
            'padx': opts.get('padx', self.padx),
            'pady': opts.get('pady', self.pady),
            '_seq': _next_pack_seq(),
        }
        self._root().redraw()

    def pack_forget(self):
        self._pack = None
        self._root().redraw()

    def pack_info(self):
        return dict(self._pack) if self._pack else {}

    def place(self, x, y):
        """Pin at an absolute offset inside the master (relative to the
        master's origin, like tkinter place)."""
        self._abs = (x, y)
        self._pack = None
        self._grid = None
        self._root().redraw()

    def place_forget(self):
        self._abs = None
        self._root().redraw()

    def place_info(self):
        return {'x': self._abs[0], 'y': self._abs[1]} if self._abs else {}

    def grid(self, **opts):
        """Grid geometry: row/column (required), padx/pady, sticky
        ('n'/'s'/'e'/'w' combos), rowspan/columnspan."""
        self._abs = None
        self._pack = None
        self._grid = dict(opts)
        self._root().redraw()

    def grid_forget(self):
        self._grid = None
        self._root().redraw()

    def grid_info(self):
        return dict(self._grid) if self._grid else {}

    # ---- events --------------------------------------------------------

    def bind(self, seq, func=None):
        """Bind a callback to an event sequence ('<Return>', '<Key>',
        '<KeyPress-x>', '<space>', ...).  Only keyboard sequences exist."""
        if func is None:
            cbs = self._binds.get(seq)
            return cbs[0] if cbs else None
        self._binds.setdefault(seq, []).append(func)

    def on_char(self, ch):
        return False  # False = no redraw needed

    def on_backspace(self):
        return False

    def on_key_name(self, name):
        """Named keys routed to the focused widget: up/down/home/end/
        pgup/pgdn (and any others Tk doesn't handle itself)."""
        return False

    def on_click(self, x, y, button, dbl):
        """A mouse press landed on this widget: (x, y) absolute, button
        (1=left), dbl=1 for a left double-click."""
        pass

    def activate(self):
        pass

    def focus_set(self):
        root = self._root()
        fs = root._focusables()
        if self in fs:
            root._focus_idx = fs.index(self)
            root.redraw()

    def focus_force(self):
        self.focus_set()

    # ---- winfo ---------------------------------------------------------

    def winfo_width(self):
        return self.w

    def winfo_height(self):
        return self.h

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def winfo_rootx(self):
        return self.x

    def winfo_rooty(self):
        return self.y

    def winfo_reqwidth(self):
        return _nat(self, 'w')

    def winfo_reqheight(self):
        return _nat(self, 'h')

    def winfo_exists(self):
        return not self.destroyed

    def winfo_ismapped(self):
        return not self.destroyed

    def winfo_children(self):
        return getattr(self.master, 'children', [])

    def winfo_parent(self):
        return self.master

    def winfo_screenwidth(self):
        fb = self._root().fb
        return fb.width if fb else 1280

    def winfo_screenheight(self):
        fb = self._root().fb
        return fb.height if fb else 1024

    # ---- misc ----------------------------------------------------------

    def _root(self):
        m = self.master
        while not isinstance(m, Tk):
            m = m.master
        return m

    def after(self, ms, func=None, *args):
        return self._root().after(ms, func, *args)

    def after_idle(self, func, *args):
        return self._root().after(0, func, *args)

    def update(self):
        self._root().update()

    def update_idletasks(self):
        self._root().update()

    def mainloop(self, n=0):
        self._root().mainloop(n)

    def destroy(self):
        m = self.master
        if hasattr(m, 'children'):
            try:
                m.children.remove(self)
            except ValueError:
                pass
        self.destroyed = True
        self._root().redraw()

    def lift(self):
        pass

    def lower(self):
        pass

    def grab_set(self):
        pass

    def grab_release(self):
        pass

    def bell(self):
        pass

    def wait_variable(self, var):
        pass  # cooperative OS: nothing to wait for

    def wait_window(self, win):
        pass

    def draw(self, fb, ox, oy):
        """Draw at (ox, oy); stores the position on self for hit tests."""
        self.x, self.y = ox, oy


class Label(Widget):
    def __init__(self, master, text='', fg=(235, 240, 245), bg=(40, 46, 58),
                 textvariable=None, **kw):
        super().__init__(master, **kw)
        self.text = text
        self._var = textvariable
        if self._var is not None:
            self._var._attach(self)
        self.fg = _color(fg)
        self.bg = _color(bg)
        self._opts.update(text=text, fg=fg, bg=bg)
        if textvariable is not None:
            self._opts['textvariable'] = textvariable
        self._recalc()

    def _display(self):
        if self._var is not None:
            return str(self._var.get())
        return self.text

    def _recalc(self):
        lines = self._display().split('\n')
        self._natw = max(textmod.text_width(ln) for ln in lines) + 2 * self.padx
        self._nath = len(lines) * textmod.LINE_H + 2 * self.pady
        self.w, self.h = self._natw, self._nath

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        y = oy + self.pady
        for ln in self._display().split('\n'):
            textmod.draw_text(fb, ox + self.padx, y, ln, self.fg)
            y += textmod.LINE_H

    def set_text(self, s):
        self.text = s
        self._recalc()
        self._root().redraw()


class Button(Widget):
    focusable = True

    def __init__(self, master, text='', command=None,
                 fg=(255, 255, 255), bg=(70, 95, 140), focus=(210, 130, 40),
                 textvariable=None, **kw):
        super().__init__(master, **kw)
        self.text = text
        self.command = command
        self._var = textvariable
        if self._var is not None:
            self._var._attach(self)
        self.fg = _color(fg)
        self.bg = _color(bg)
        self.focus = _color(focus)
        self._opts.update(text=text, fg=fg, bg=bg, focus=focus,
                          command=command)
        if textvariable is not None:
            self._opts['textvariable'] = textvariable
        self._recalc()

    def _display(self):
        if self._var is not None:
            return str(self._var.get())
        return self.text

    def _recalc(self):
        lines = self._display().split('\n')
        self._natw = max(textmod.text_width(ln) for ln in lines) + 2 * self.padx + 6
        self._nath = len(lines) * textmod.LINE_H + 2 * self.pady
        self.w, self.h = self._natw, self._nath

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        bg = self.focus if self.focused else self.bg
        fb.fill_rect(ox, oy, self.w, self.h, *bg)
        y = oy + self.pady
        for ln in self._display().split('\n'):
            textmod.draw_text(fb, ox + self.padx + 3, y, ln, self.fg)
            y += textmod.LINE_H
        fb.draw_rect(ox, oy, self.w, self.h, *self.fg)

    def activate(self):
        if self.command is not None:
            try:
                self.command()
            except Exception as e:
                kern.write('tk: button "%s" command error: %s\n' % (self.text, e))
        kern.write('tk: button "%s" clicked\n' % self.text)

    def on_click(self, x, y, button, dbl):
        if button == 1:
            self.activate()


class Entry(Widget):
    """Single-line text entry.  Tab focuses it, printable characters insert
    at the end, Backspace deletes, Enter fires '<Return>' bindings."""

    focusable = True

    def __init__(self, master, textvariable=None, width=20, show=None,
                 fg=(235, 240, 245), bg=(40, 46, 58),
                 insertbackground=(150, 190, 235), **kw):
        super().__init__(master, **kw)
        self.width = width
        self.show = show
        self.fg = _color(fg)
        self.bg = _color(bg)
        self.cursor_color = _color(insertbackground)
        self._var = textvariable
        self._text = ''
        if self._var is not None:
            self._var._attach(self)
            self._text = self._var.get()
        self._opts.update(width=width, fg=fg, bg=bg,
                          insertbackground=insertbackground)
        self._recalc()

    def _recalc(self):
        self._natw = max(self.width, 1) * textmod.char_width('0')
        self._nath = textmod.LINE_H + 2 * self.pady
        self.w, self.h = self._natw, self._nath

    def _display(self):
        s = self._var.get() if self._var is not None else self._text
        if self.show:
            s = self.show * len(s)
        return s

    def get(self):
        return self._var.get() if self._var is not None else self._text

    def set(self, s):
        if self._var is not None:
            self._var.set(s)
        else:
            self._text = s
            self._root().redraw()

    def _norm_index(self, i, n):
        if i == 'end' or i == 'insert':
            return n
        try:
            i = int(i)
        except (TypeError, ValueError):
            return n
        return max(0, min(i, n))

    def insert(self, index, s):
        cur = self.get()
        i = self._norm_index(index, len(cur))
        self.set(cur[:i] + s + cur[i:])

    def delete(self, first, last=None):
        cur = self.get()
        i0 = self._norm_index(first, len(cur))
        if last is None:
            i1 = i0 + 1
        else:
            i1 = self._norm_index(last, len(cur))
        if i1 < i0:
            i1 = i0
        self.set(cur[:i0] + cur[i1:])

    def on_char(self, ch):
        if ch and 32 <= ord(ch) < 127:
            self.set(self.get() + ch)
            return True
        return False

    def on_backspace(self):
        cur = self.get()
        if cur:
            self.set(cur[:-1])
            return True
        return False

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        fb.draw_rect(ox, oy, self.w, self.h,
                     *self.cursor_color if self.focused else (90, 100, 115))
        s = self._display()
        if s:
            textmod.draw_text(fb, ox + 4, oy + self.pady, s, self.fg)
        if self.focused:
            cx = ox + 4 + textmod.text_width(s)
            fb.fill_rect(cx + 1, oy + 2, 2, textmod.LINE_H - 4,
                         *self.cursor_color)


class Frame(Widget):
    """Container: children are laid out inside its own region with the same
    pack/grid/place machinery.  Without an explicit width/height it
    shrink-wraps its children."""

    def __init__(self, master, width=None, height=None, bg=None,
                 bd=0, relief='flat', **kw):
        super().__init__(master, **kw)
        self.children = []
        self.bg = _color(bg) if bg is not None else None
        self.bd = bd
        self.relief = relief
        self._want_w = width
        self._want_h = height
        self._opts.update(width=width, height=height, bd=bd, relief=relief)
        self._recalc()

    def add(self, w):
        self.children.append(w)
        return w

    def redraw(self):
        self._root().redraw()

    def _recalc(self):
        nw, nh = self._natural()
        self._natw, self._nath = nw, nh
        self.w, self.h = nw, nh

    def _natural(self):
        nw, nh = _natural_size(self.children)
        if self._want_w is not None:
            nw = self._want_w
        if self._want_h is not None:
            nh = self._want_h
        return nw, nh

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        if self.bg is not None:
            fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        if self.bd:
            fb.draw_rect(ox, oy, self.w, self.h, *(110, 125, 150))
        placements = _layout(self, (ox, oy, self.w, self.h), (ox, oy),
                             self.children)
        for w in self.children:
            if w in placements:
                px, py, pw, ph = placements[w]
                w.x, w.y, w.w, w.h = px, py, pw, ph
                w.draw(fb, px, py)


class Canvas(Widget):
    """A drawing surface.  Items are recorded and repainted on every redraw,
    so the canvas survives full-screen refreshes.

    tkinter-style items: create_line(x0,y0,x1,y1, fill=, width=),
    create_rectangle(x0,y0,x1,y1, fill=, outline=, width=),
    create_oval(...), create_polygon(x0,y0,...), create_text(x,y,text=,
    fill=, anchor=) -- plus the legacy Ripos forms create_rect(x,y,w,h,
    color=, outline=) and create_line(..., color=).  Item ids are 1-based;
    delete(id) and delete('all') remove items."""

    def __init__(self, master, width=100, height=100, bg=(30, 36, 48), **kw):
        super().__init__(master, **kw)
        self.bg = _color(bg)
        self._natw = width
        self._nath = height
        self.w, self.h = self._natw, self._nath
        self._items = []
        self._opts.update(width=width, height=height, bg=self.bg)

    def _recalc(self):
        self._natw, self._nath = self.w, self.h

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        for it in self._items:
            if callable(it):
                it(fb, ox, oy)
            else:
                self._draw_item(fb, ox, oy, it)

    def clear(self):
        self._items = []
        self._root().redraw()

    def delete(self, *args):
        for a in args:
            if a == 'all':
                self._items = []
            elif isinstance(a, int):
                i = a - 1
                if 0 <= i < len(self._items):
                    del self._items[i]
        self._root().redraw()

    # ---- item drawing --------------------------------------------------

    def _draw_item(self, fb, ox, oy, it):
        k = it['kind']
        if k == 'line':
            self._polyline(fb, ox, oy, it['pts'], it['fill'], it['width'])
        elif k == 'rect':
            x = min(it['x0'], it['x1']) + ox
            y = min(it['y0'], it['y1']) + oy
            w = abs(it['x1'] - it['x0'])
            h = abs(it['y1'] - it['y0'])
            if it['fill'] is not None:
                fb.fill_rect(x, y, w, h, *it['fill'])
            o = it['outline']
            wd = max(it['width'], 1)
            if o is not None:
                fb.fill_rect(x, y, w, wd, *o)
                fb.fill_rect(x, y + h - wd, w, wd, *o)
                fb.fill_rect(x, y, wd, h, *o)
                fb.fill_rect(x + w - wd, y, wd, h, *o)
        elif k == 'oval':
            self._oval(fb, ox, oy, it)
        elif k == 'poly':
            self._polygon(fb, ox, oy, it)
        elif k == 'text':
            x, y = it['x'] + ox, it['y'] + oy
            s = it['text']
            tw = textmod.text_width(s)
            th = textmod.LINE_H
            a = it['anchor']
            if a in ('n', 's', 'center'):
                dx = -tw // 2
            elif a in ('e', 'ne', 'se'):
                dx = -tw
            else:
                dx = 0
            if a in ('w', 'e', 'center'):
                dy = -th // 2
            elif a in ('s', 'sw', 'se'):
                dy = -th
            else:
                dy = 0
            textmod.draw_text(fb, x + dx, y + dy, s, it['fill'])

    def _polyline(self, fb, ox, oy, pts, color, width):
        xs = [ox + pts[i] for i in range(0, len(pts), 2)]
        ys = [oy + pts[i + 1] for i in range(0, len(pts), 2)]
        if width <= 1:
            for i in range(len(xs) - 1):
                fb.line(xs[i], ys[i], xs[i + 1], ys[i + 1], *color)
            return
        off_x, off_y = (0, 1)
        if len(xs) >= 2 and abs(xs[1] - xs[0]) < abs(ys[1] - ys[0]):
            off_x, off_y = (1, 0)
        for k in range(width):
            o = k - (width - 1) // 2
            for i in range(len(xs) - 1):
                fb.line(xs[i] + o * off_x, ys[i] + o * off_y,
                        xs[i + 1] + o * off_x, ys[i + 1] + o * off_y, *color)

    def _oval(self, fb, ox, oy, it):
        xa = min(it['x0'], it['x1']) + ox
        xb = max(it['x0'], it['x1']) + ox
        ya = min(it['y0'], it['y1']) + oy
        yb = max(it['y0'], it['y1']) + oy
        cx = (xa + xb) / 2.0
        cy = (ya + yb) / 2.0
        rx = (xb - xa) / 2.0
        ry = (yb - ya) / 2.0
        fill = it['fill']
        outline = it['outline']
        if rx <= 0 or ry <= 0:
            if fill is not None:
                fb.fill_rect(xa, ya, max(xb - xa, 1), max(yb - ya, 1), *fill)
            return
        for yy in range(ya, yb + 1):
            t = (yy + 0.5 - cy) / ry
            if t < -1 or t > 1:
                continue
            half = rx * (1.0 - t * t) ** 0.5
            xl = int(cx - half + 0.5)
            xr = int(cx + half)
            if fill is not None and xr >= xl:
                fb.fill_rect(xl, yy, xr - xl + 1, 1, *fill)
            if outline is not None:
                fb.set_pixel(xl, yy, *outline)
                fb.set_pixel(xr, yy, *outline)

    def _polygon(self, fb, ox, oy, it):
        pts = it['pts']
        xs = [ox + pts[i] for i in range(0, len(pts), 2)]
        ys = [oy + pts[i + 1] for i in range(0, len(pts), 2)]
        n = len(xs)
        if n < 3:
            return
        fill = it['fill']
        outline = it['outline']
        ymin, ymax = min(ys), max(ys)
        for yy in range(ymin, ymax + 1):
            cross = []
            for i in range(n):
                x0, y0 = xs[i], ys[i]
                x1, y1 = xs[(i + 1) % n], ys[(i + 1) % n]
                if (y0 <= yy < y1) or (y1 <= yy < y0):
                    cross.append(x0 + (yy - y0) * (x1 - x0) / (y1 - y0))
            cross.sort()
            if fill is not None:
                for k in range(0, len(cross) - 1, 2):
                    a = int(cross[k] + 0.5)
                    b = int(cross[k + 1] + 0.5)
                    fb.fill_rect(a, yy, max(b - a, 1), 1, *fill)
        if outline is not None:
            for i in range(n):
                fb.line(xs[i], ys[i], xs[(i + 1) % n], ys[(i + 1) % n],
                        *outline)

    # ---- item creation -------------------------------------------------

    def create_line(self, *args, **kw):
        if 'color' in kw:   # legacy Ripos form
            c = _color(kw['color'])
            a = args
            self._items.append(lambda fb, ox, oy, a=a, c=c:
                               self._polyline(fb, ox, oy, a, c, 1))
            return len(self._items)
        it = {'kind': 'line', 'pts': list(args),
              'fill': _color(kw.get('fill', 'black')),
              'width': max(int(kw.get('width', 1)), 1)}
        self._items.append(it)
        return len(self._items)

    def create_rect(self, x, y, w, h, color=None, outline=(200, 210, 230)):
        """Legacy Ripos rectangle (x, y, w, h); prefer create_rectangle."""
        if color is not None:
            c = _color(color)
            self._items.append(
                lambda fb, ox, oy, x=x, y=y, w=w, h=h, c=c:
                fb.fill_rect(ox + x, oy + y, w, h, *c))
        o = _color(outline)
        self._items.append(
            lambda fb, ox, oy, x=x, y=y, w=w, h=h, o=o:
            fb.draw_rect(ox + x, oy + y, w, h, *o))
        return self._items[-1]

    def create_rectangle(self, x0, y0, x1, y1, fill=None, outline='black',
                         width=1, **kw):
        fill_c = _color(fill) if fill is not None else None
        outline_c = _color(outline) if outline is not None else None
        it = {'kind': 'rect', 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
              'fill': fill_c, 'outline': outline_c,
              'width': max(int(width), 1)}
        self._items.append(it)
        return len(self._items)

    def create_oval(self, x0, y0, x1, y1, fill=None, outline='black',
                    width=1, **kw):
        fill_c = _color(fill) if fill is not None else None
        outline_c = _color(outline) if outline is not None else None
        self._items.append({'kind': 'oval', 'x0': x0, 'y0': y0, 'x1': x1,
                            'y1': y1, 'fill': fill_c, 'outline': outline_c,
                            'width': max(int(width), 1)})
        return len(self._items)

    def create_polygon(self, *args, **kw):
        fill = kw.get('fill')
        outline = kw.get('outline')
        fill_c = _color(fill) if fill is not None else None
        outline_c = _color(outline) if outline is not None else None
        self._items.append({'kind': 'poly', 'pts': list(args),
                            'fill': fill_c, 'outline': outline_c})
        return len(self._items)

    def create_text(self, *args, **kw):
        if len(args) >= 3 and isinstance(args[2], str):
            # legacy Ripos form: create_text(x, y, s, color=...)
            x, y, s = args[0], args[1], args[2]
            c = _color(kw.get('color', (235, 240, 245)))
            self._items.append(lambda fb, ox, oy, x=x, y=y, s=s, c=c:
                               textmod.draw_text(fb, ox + x, oy + y, s, c))
            return len(self._items)
        self._items.append({'kind': 'text', 'x': args[0], 'y': args[1],
                            'text': kw.get('text', ''),
                            'fill': _color(kw.get('fill', 'black')),
                            'anchor': kw.get('anchor', 'center')})
        return len(self._items)

    def create_pixel(self, x, y, color=(255, 255, 255)):
        c = _color(color)
        self._items.append(
            lambda fb, ox, oy, x=x, y=y, c=c:
            fb.set_pixel(ox + x, oy + y, *c))
        return self._items[-1]




class Listbox(Widget):
    """A list box: rows of (label, meta) items with keyboard and mouse
    selection.  Up/Down/Home/End/PageUp/PageDown move the selection,
    Enter/Space or a double-click calls on_activate(), and a single
    left click selects.  Items are strings or (label, meta) tuples;
    meta is drawn right-aligned (Windows-Explorer style)."""

    focusable = True

    def __init__(self, master, height=10, fg=(235, 240, 245),
                 bg=(30, 36, 48), sel=(70, 95, 140), meta=(150, 165, 180),
                 on_activate=None, **kw):
        super().__init__(master, **kw)
        self.items = []
        self.height = height
        self.fg = _color(fg)
        self.bg = _color(bg)
        self.sel = _color(sel)
        self.meta_color = _color(meta)
        self.selected = -1
        self._top = 0
        self.on_activate = on_activate
        self._natw = 120
        self._nath = height * textmod.LINE_H + 2 * self.pady
        self.w, self.h = self._natw, self._nath
        self._opts.update(height=height, fg=self.fg, bg=self.bg)

    def _recalc(self):
        w = 120
        for it in self.items:
            label, meta = self._split(it)
            w = max(w, textmod.text_width(label) + textmod.text_width(meta) + 12)
        self._natw = w + 2 * self.padx
        self._nath = self.height * textmod.LINE_H + 2 * self.pady
        self.w, self.h = self._natw, self._nath

    @staticmethod
    def _split(it):
        if isinstance(it, (tuple, list)) and len(it) == 2:
            return str(it[0]), str(it[1])
        return str(it), ""

    def set_items(self, items):
        self.items = list(items)
        self.selected = -1
        self._top = 0
        self._recalc()
        self._root().redraw()

    def selection(self):
        return self.selected if 0 <= self.selected < len(self.items) else -1

    def get(self):
        i = self.selection()
        return self.items[i] if i >= 0 else None

    def _ensure_visible(self):
        if self.selected < self._top:
            self._top = max(0, self.selected)
        if self.selected >= self._top + self.height:
            self._top = max(0, self.selected - self.height + 1)

    def on_key_name(self, name):
        n = len(self.items)
        if n == 0:
            return False
        if name == "up":
            self.selected = n - 1 if self.selected < 0 else max(0, self.selected - 1)
        elif name == "down":
            self.selected = 0 if self.selected < 0 else min(n - 1, self.selected + 1)
        elif name == "home":
            self.selected = 0
        elif name == "end":
            self.selected = n - 1
        elif name == "pgup":
            self.selected = max(0, (self.selected if self.selected >= 0 else 0) - self.height)
        elif name == "pgdn":
            self.selected = min(n - 1, (self.selected if self.selected >= 0 else -1) + self.height)
        else:
            return False
        self._ensure_visible()
        return True

    def activate(self):
        if self.on_activate is not None and self.selection() >= 0:
            self.on_activate(self.selection(), self.get())

    def on_click(self, x, y, button, dbl):
        if button != 1:
            return
        row = (y - self.y - self.pady) // textmod.LINE_H + self._top
        if 0 <= row < len(self.items):
            self.selected = row
            if dbl:
                self.activate()

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        meta_w = 0
        rows = []
        for i in range(self._top, min(self._top + self.height, len(self.items))):
            label, meta = self._split(self.items[i])
            rows.append((i, label, meta))
            meta_w = max(meta_w, textmod.text_width(meta))
        for k, (i, label, meta) in enumerate(rows):
            y = oy + self.pady + k * textmod.LINE_H
            if i == self.selected:
                fb.fill_rect(ox + 1, y, self.w - 2, textmod.LINE_H - 2, *self.sel)
            textmod.draw_text(fb, ox + self.padx, y, label, self.fg)
            if meta:
                mx = ox + self.w - self.padx - meta_w
                textmod.draw_text(fb, mx, y, meta, self.meta_color)
        fb.draw_rect(ox, oy, self.w, self.h, *self.meta_color)
class Tk:
    """The widget root: owns the framebuffer, keyboard, layout and focus."""

    def __init__(self, title='Ripos', bg=(22, 26, 34), show_title=True, **kw):
        self._title = title
        self.bg = _color(bg)
        self.show_title = show_title
        for k, v in kw.items():   # tolerate real-tkinter args (className,
            self._opts[k] = v     # screenName, sync, ...) without crashing
        self.fb = None
        self.kb = None
        self.mouse = None
        self.children = []
        self._focus_idx = 0
        self.destroyed = False
        self._embedded = False   # True when hosted inside a wm.Window
        self._binds = {}         # root-level (bind_all) sequences
        self._opts = {}
        self._geom = None
        self._protocols = {}
        self._withdrawn = False
        global _LAST_ROOT
        _LAST_ROOT = self

    # ---- setup ------------------------------------------------------

    def start(self, fb, kb, mouse=None):
        global _ACTIVE_ROOT
        self.fb = fb
        self.kb = kb
        self.mouse = mouse
        self.kb.on_event(self.on_key)
        if mouse is not None:
            mouse.on_event(self.on_mouse)
        _ACTIVE_ROOT = self
        self.redraw()

    def activate(self):
        """Make this root the active app (e.g. when a nested app quits back
        to it) and re-arm its redraw tick."""
        global _ACTIVE_ROOT
        if self.destroyed:
            return
        _ACTIVE_ROOT = self
        self.redraw()
        self._tick()

    def _ensure(self):
        if self.fb is None and not self._embedded:
            from framebuffer import Framebuffer
            from keyboard import Keyboard
            self.start(Framebuffer(), Keyboard())

    def add(self, w):
        self.children.append(w)
        return w

    # ---- option access ------------------------------------------------

    def configure(self, **kw):
        for k, v in kw.items():
            self._opts[k] = v
        self.redraw()

    config = configure

    def cget(self, name):
        return self._opts.get(name)

    def __getitem__(self, name):
        return self.cget(name)

    def __setitem__(self, name, value):
        self.configure(**{name: value})

    # ---- window management ---------------------------------------------

    def title(self, t=None):
        """Get/set the window title (shown in the root title bar)."""
        if t is not None:
            self._title = t
            self.redraw()
        return self._title

    def geometry(self, g=None):
        """Get/set the geometry string.  The Ripos window is the whole
        screen, so setting it is accepted but has no visual effect."""
        if g is not None:
            self._geom = g
        if self._geom:
            return self._geom
        if self.fb is not None:
            return '%dx%d+0+0' % (self.fb.width, self.fb.height)
        return '1x1+0+0'

    def resizable(self, width=None, height=None):
        pass  # full-screen; accepted for compatibility

    def withdraw(self):
        self._withdrawn = True

    def iconify(self):
        self._withdrawn = True

    def deiconify(self):
        self._withdrawn = False
        self.redraw()

    def state(self):
        return 'normal'

    def attributes(self, *args):
        return None

    def overrideredirect(self, flag=None):
        return False

    def protocol(self, name, func=None):
        """Store a WM protocol handler ('WM_DELETE_WINDOW' etc.); the
        WM_DELETE_WINDOW handler, if any, is called by destroy()."""
        if func is None:
            return self._protocols.get(name)
        self._protocols[name] = func

    # ---- timers / events ------------------------------------------------

    def after(self, ms, func=None, *args):
        """Schedule func(*args) after ms.  Returns the callback (usable as
        a token for after_cancel)."""
        if func is None:
            return None
        kern.after(int(ms), lambda: func(*args))
        return func

    def after_idle(self, func, *args):
        return self.after(0, func, *args)

    def after_cancel(self, ident):
        pass  # kernel timers are one-shot; a cancelled id is a no-op

    def update(self):
        self._ensure()
        if self.fb is not None:
            self.redraw()

    def update_idletasks(self):
        self.update()

    def bind(self, seq, func=None):
        if func is None:
            cbs = self._binds.get(seq)
            return cbs[0] if cbs else None
        self._binds.setdefault(seq, []).append(func)

    bind_all = bind

    # ---- keyboard ---------------------------------------------------

    def _walk(self, w):
        yield w
        if isinstance(w, Frame):
            for c in w.children:
                yield from self._walk(c)

    def _focusables(self):
        out = []
        for w in self.children:
            for x in self._walk(w):
                if x.focusable:
                    out.append(x)
        return out

    def _focused(self):
        fs = self._focusables()
        if not fs:
            return None
        return fs[self._focus_idx % len(fs)]

    def _fire_bind(self, w, seq, char=None):
        """Run the callback(s) bound to seq on w (or the root).  Returns
        True if something handled the event."""
        if w is not None and seq in w._binds:
            ev = _Event(char, seq, w)
            for cb in w._binds[seq]:
                cb(ev)
            return True
        if seq in self._binds:
            ev = _Event(char, seq, None)
            for cb in self._binds[seq]:
                cb(ev)
            return True
        return False

    def on_key(self, ev):
        if self.destroyed:
            return
        name, ch, pressed = ev
        if not pressed:
            return
        w = self._focused()
        if name == 'tab':
            if not self._fire_bind(w, '<Tab>'):
                fs = self._focusables()
                if fs:
                    self._focus_idx = (self._focus_idx + 1) % len(fs)
                    self.redraw()
            return
        if name == 'enter':
            if not self._fire_bind(w, '<Return>'):
                if w is not None:
                    w.activate()
            self.redraw()
            return
        if name == 'esc':
            self._fire_bind(None, '<Escape>')
            return
        if name in ('up', 'down', 'left', 'right', 'home', 'end',
                    'pgup', 'pgdn', 'del', 'ins'):
            if w is not None and w.on_key_name(name):
                self.redraw()
            return
        if name.startswith('F') and len(name) <= 3:
            # F1..F12: root-level binds like '<F5>'
            self._fire_bind(None, '<%s>' % name)
            return
        if name == 'backspace':
            if w is not None and w.on_backspace():
                self.redraw()
            return
        if name == 'space':
            if isinstance(w, Entry):
                if w.on_char(' '):
                    self.redraw()
            elif w is not None:
                w.activate()
                self.redraw()
            return
        if ch is None or ch == 0:
            return
        if 32 <= ch < 127:
            c = chr(ch)
            if (self._fire_bind(w, '<Key>', c)
                    or self._fire_bind(w, '<KeyPress>', c)
                    or self._fire_bind(w, '<KeyPress-%s>' % c, c)):
                return
            if w is not None and w.on_char(c):
                self.redraw()

    # ---- mouse ----------------------------------------------------

    def _hit(self, w, x, y):
        """Deepest widget at (x, y): frames recurse into their children
        first (topmost last-added child wins), then the frame itself."""
        if isinstance(w, Frame):
            for c in reversed(w.children):
                r = self._hit(c, x, y)
                if r is not None:
                    return r
            if w.x <= x < w.x + w.w and w.y <= y < w.y + w.h:
                return w
            return None
        if w.x <= x < w.x + w.w and w.y <= y < w.y + w.h:
            return w
        return None

    def _hit_test(self, x, y):
        for w in reversed(self.children):
            r = self._hit(w, x, y)
            if r is not None:
                return r
        return None

    def on_mouse(self, ev):
        """Mouse event (x, y, button, pressed, dbl) from mouse.Mouse."""
        if self.destroyed:
            return
        if _ACTIVE_ROOT is not self:
            return  # another app owns the screen
        x, y, button, pressed, dbl = ev
        if button != 1 or not pressed:
            return
        target = self._hit_test(x, y)
        if target is not None:
            if target.focusable:
                fs = self._focusables()
                if target in fs:
                    self._focus_idx = fs.index(target)
            target.on_click(x, y, button, dbl)
            self.redraw()

    def handle_char(self, ch):
        """Route one character (wm on_char style) to the focused widget.
        Returns True if a redraw is needed; standalone roots redraw
        themselves, embedded roots leave it to the wm desktop."""
        if self.destroyed:
            return False
        w = self._focused()
        redraw = False
        if ch == '\n':
            if not self._fire_bind(w, '<Return>'):
                if w is not None:
                    w.activate()
            redraw = True
        elif ch == '\b':
            if w is not None and w.on_backspace():
                redraw = True
        elif ch == ' ' and isinstance(w, Entry):
            if w.on_char(' '):
                redraw = True
        else:
            if (w is not None
                    and not self._fire_bind(w, '<Key>', ch)
                    and w.on_char(ch)):
                redraw = True
        if redraw and not self._embedded:
            self.redraw()
        return redraw

    # ---- rendering --------------------------------------------------

    def redraw(self):
        self._ensure()
        if self.fb is None:
            return  # embedded root: the wm desktop redraws via draw_region
        self.draw_region(self.fb, 0, 0, self.fb.width, self.fb.height)

    def _mark_focus(self):
        target = self._focused()
        for w in self.children:
            self._set_focus_flag(w, False)
        if target is not None:
            target.focused = True

    def _set_focus_flag(self, w, flag):
        if isinstance(w, Frame):
            for c in w.children:
                self._set_focus_flag(c, flag)
        else:
            w.focused = flag

    def draw_region(self, fb, ox, oy, rw, rh):
        """Draw the whole tree into (ox, oy)-(ox+rw, oy+rh) of fb."""
        fb.fill_rect(ox, oy, rw, rh, *self.bg)
        y = oy
        if self.show_title:
            fb.fill_rect(ox, y, rw, wm.TITLE_H, *wm.TITLE_BG)
            textmod.draw_text(fb, ox + 4, y + 2, self._title, wm.TITLE_TEXT)
            y += wm.TITLE_H
        self._mark_focus()
        cavity = (ox, y, rw, rh - (y - oy))
        placements = _layout(self, cavity, (ox, oy), self.children)
        for w in self.children:
            if w in placements:
                px, py, pw, ph = placements[w]
                w.x, w.y, w.w, w.h = px, py, pw, ph
                w.draw(fb, px, py)
        if self.mouse is not None:
            self.mouse.draw_cursor(fb)

    # ---- lifecycle --------------------------------------------------

    def mainloop(self, n=0):
        """Hand control to the kernel event loop; returns immediately."""
        self._ensure()
        kern.write('tk: mainloop started (%dx%d, %d widgets)\n'
                   % (self.fb.width, self.fb.height, len(self.children)))
        self.redraw()
        self._tick()

    def _tick(self):
        if self.destroyed:
            return
        if _ACTIVE_ROOT is not self:
            return  # another app owns the screen; stop ticking
        self.redraw()
        kern.after(1000, self._tick)

    def destroy(self):
        if self.destroyed:
            return
        cb = self._protocols.get('WM_DELETE_WINDOW')
        if cb is not None:
            try:
                cb()
            except Exception as e:
                kern.write('tk: WM_DELETE_WINDOW handler error: %s\n' % e)
        self.destroyed = True
        kern.write('tk: destroyed\n')


class _Event:
    """Minimal event object handed to bind() callbacks."""

    def __init__(self, char, sequence, widget):
        self.char = char
        self.keysym = sequence
        self.keysym_num = ord(char) if char else 0
        self.widget = widget

    def __repr__(self):
        return '<tk event %s char=%r>' % (self.keysym, self.char)


_LAST_ROOT = None   # the most recently created Tk root (for tkinter.mainloop)

# The root that currently owns the screen.  Only it redraws on its 1 s tick
# and processes mouse clicks, so apps can hand the screen to each other
# (shell -> fm -> editor -> fm) without the old root repainting over the
# new one.  set by start()/activate(); cleared when an app quits to the
# shell (no tk root owns the screen then).
_ACTIVE_ROOT = None


class WidgetWindow(wm.Window):
    """Host a tk widget tree inside a wm window (uses the draw_content hook
    in wm.Desktop._draw_window, so it coexists with the other windows)."""

    def __init__(self, title_key, x, y, w, h, root):
        super().__init__(title_key, x, y, w, h)
        self.root = root
        root.show_title = False
        root._embedded = True

    def draw_content(self, fb, x, y, w, h):
        self.root.draw_region(fb, x, y, w, h)

    def on_char(self, ch):
        return self.root.handle_char(ch)
