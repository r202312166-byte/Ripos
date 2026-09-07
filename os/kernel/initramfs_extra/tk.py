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
import appbar
import settings
from framebuffer import Framebuffer   # _ShadowFB offscreen surface

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
    # pass 1: fixed bands.  top/left bands grow from the top/left edge in
    # pack order; bottom/right bands grow from the bottom/right edge upward
    # / leftward (true tkinter parcel model).  expand= widgets are deferred
    # to pass 2 so they never push fixed bands around.
    for w in order:
        o = w._pack
        if o.get('expand'):
            continue
        side = o['side']
        fill = o['fill']
        anchor = o['anchor']
        px0, px1 = _pad_pair(o['padx'])
        py0, py1 = _pad_pair(o['pady'])
        nw = _nat(w, 'w')
        nh = _nat(w, 'h')
        if side == 'top':
            aw = nw
            if fill in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            ax = cx + px0
            if anchor == 'center':
                ax = cx + (cw - px0 - px1 - aw) // 2 + px0
            elif anchor == 'e':
                ax = cx + cw - px0 - aw
            ah = nh
            if fill in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            placements[w] = (ax, cy + py0, aw, ah)
            cy += nh + py0 + py1
            ch -= nh + py0 + py1
        elif side == 'bottom':
            aw = nw
            if fill in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            ax = cx + px0
            if anchor == 'center':
                ax = cx + (cw - px0 - px1 - aw) // 2 + px0
            elif anchor == 'e':
                ax = cx + cw - px0 - aw
            ah = nh
            if fill in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            placements[w] = (ax, cy + ch - ah - py1, aw, ah)
            ch -= nh + py0 + py1
        elif side == 'left':
            ah = nh
            if fill in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            ay = cy + py0
            if anchor == 'center':
                ay = cy + (ch - py0 - py1 - ah) // 2 + py0
            elif anchor == 's':
                ay = cy + ch - py0 - ah
            aw = nw
            if fill in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            placements[w] = (cx + px0, ay, aw, ah)
            cx += nw + px0 + px1
            cw -= nw + px0 + px1
        else:  # right: grows from the right edge leftward
            ah = nh
            if fill in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            ay = cy + py0
            if anchor == 'center':
                ay = cy + (ch - py0 - py1 - ah) // 2 + py0
            elif anchor == 's':
                ay = cy + ch - py0 - ah
            aw = nw
            if fill in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            placements[w] = (cx + cw - aw - px1, ay, aw, ah)
            cw -= nw + px0 + px1
    # pass 2: expand= widgets split the leftover middle cavity, in pack
    # order; fill stretches them inside their parcel.
    vexp = [w for w in order
            if w._pack.get('expand') and w._pack['side'] in ('top', 'bottom')]
    hexp = [w for w in order
            if w._pack.get('expand') and w._pack['side'] in ('left', 'right')]
    if vexp and ch > 0:
        each = ch // len(vexp)
        rem = ch % len(vexp)
        cur = cy
        for i, w in enumerate(vexp):
            o = w._pack
            px0, px1 = _pad_pair(o['padx'])
            py0, py1 = _pad_pair(o['pady'])
            nw = _nat(w, 'w')
            nh = _nat(w, 'h')
            aw = nw
            if o['fill'] in ('x', 'both'):
                aw = max(0, cw - px0 - px1)
            ax = cx + px0
            if o['anchor'] == 'center':
                ax = cx + (cw - px0 - px1 - aw) // 2 + px0
            elif o['anchor'] == 'e':
                ax = cx + cw - px0 - aw
            end = cur + each + (1 if i < rem else 0)
            ah = nh
            if o['fill'] in ('y', 'both'):
                ah = max(0, end - cur - py0 - py1)
            placements[w] = (ax, cur + py0, aw, ah)
            cur = end
    if hexp and cw > 0:
        each = cw // len(hexp)
        rem = cw % len(hexp)
        cur = cx
        for i, w in enumerate(hexp):
            o = w._pack
            px0, px1 = _pad_pair(o['padx'])
            py0, py1 = _pad_pair(o['pady'])
            nw = _nat(w, 'w')
            nh = _nat(w, 'h')
            ah = nh
            if o['fill'] in ('y', 'both'):
                ah = max(0, ch - py0 - py1)
            ay = cy + py0
            if o['anchor'] == 'center':
                ay = cy + (ch - py0 - py1 - ah) // 2 + py0
            elif o['anchor'] == 's':
                ay = cy + ch - py0 - ah
            end = cur + each + (1 if i < rem else 0)
            aw = nw
            if o['fill'] in ('x', 'both'):
                aw = max(0, end - cur - px0 - px1)
            placements[w] = (cur + px0, ay, aw, ah)
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

    def on_click(self, x, y, button, dbl, clicks=1):
        """A mouse press landed on this widget: (x, y) absolute, button
        (1=left, 2=right, 4=middle), dbl=1 for a double/triple-click,
        clicks = 1/2/3 (single/double/triple)."""
        pass

    def on_drag(self, x, y):
        """Mouse moved while the left button is held (started on this
        widget with on_click)."""
        pass

    def on_release(self, x, y):
        """Left button released after a press/drag on this widget."""
        pass

    def on_wheel(self, delta):
        """Mouse wheel moved (delta = signed scroll steps); True = redraw."""
        return False

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
    def __init__(self, master, text='', fg=None, bg=None,
                 textvariable=None, **kw):
        super().__init__(master, **kw)
        self.text = text
        self._var = textvariable
        if self._var is not None:
            self._var._attach(self)
        if fg is None:
            fg = settings.c('panel_fg')
        if bg is None:
            bg = settings.c('label_bg')
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
                 fg=None, bg=None, focus=None,
                 textvariable=None, **kw):
        super().__init__(master, **kw)
        self.text = text
        self.command = command
        self._var = textvariable
        if self._var is not None:
            self._var._attach(self)
        if fg is None:
            fg = settings.c('title_fg')
        if bg is None:
            bg = settings.c('title_bg')
        if focus is None:
            focus = settings.c('focus')
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

    def on_click(self, x, y, button, dbl, clicks=1):
        if button == 1:
            self.activate()


class Entry(Widget):
    """Single-line text entry.  Tab focuses it, printable characters insert
    at the end, Backspace deletes, Enter fires '<Return>' bindings."""

    focusable = True

    def __init__(self, master, textvariable=None, width=20, show=None,
                 fg=None, bg=None,
                 insertbackground=None, **kw):
        super().__init__(master, **kw)
        self.width = width
        self.show = show
        if fg is None:
            fg = settings.c('panel_fg')
        if bg is None:
            bg = settings.c('input_bg')
        if insertbackground is None:
            insertbackground = settings.c('cursor')
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

    def __init__(self, master, width=100, height=100, bg=None, **kw):
        super().__init__(master, **kw)
        if bg is None:
            bg = settings.c('canvas_bg')
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

    def __init__(self, master, height=10, fg=None,
                 bg=None, sel=None, meta=None,
                 on_activate=None, on_select=None, **kw):
        super().__init__(master, **kw)
        self.items = []
        self.height = height
        if fg is None:
            fg = settings.c('panel_fg')
        if bg is None:
            bg = settings.c('panel')
        if sel is None:
            sel = settings.c('sel')
        if meta is None:
            meta = settings.c('status_fg')
        self.fg = _color(fg)
        self.bg = _color(bg)
        self.sel = _color(sel)
        self.meta_color = _color(meta)
        self.selected = -1
        self._top = 0
        self.on_activate = on_activate
        self.on_select = on_select
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
        if self.on_select is not None:
            self.on_select()
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
        if self.on_select is not None:
            self.on_select()
        return True

    def activate(self):
        if self.on_activate is not None and self.selection() >= 0:
            self.on_activate(self.selection(), self.get())

    def on_click(self, x, y, button, dbl, clicks=1):
        if button != 1:
            return
        row = (y - self.y - self.pady) // textmod.LINE_H + self._top
        if 0 <= row < len(self.items):
            self.selected = row
            if self.on_select is not None:
                self.on_select()
            if dbl:
                self.activate()

    def on_wheel(self, delta):
        n = len(self.items)
        if n == 0:
            return False
        step = 3 if delta > 0 else -3
        self.selected = max(0, min(n - 1,
                                   (self.selected if self.selected >= 0 else 0)
                                   + step))
        self._ensure_visible()
        return True

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


class Scrollbar(Widget):
    """A scrollbar: a track with a thumb whose size reflects the visible
    fraction (0..1) of the controlled widget.  Vertical or horizontal.
    The controlled widget calls set(first, last) after scrolling and
    passes command= to receive scroll requests; command is called with
    the requested first fraction (0..1)."""

    focusable = False

    def __init__(self, master, orient='vertical', command=None,
                 fg=None, bg=None, thumb=None,
                 **kw):
        super().__init__(master, **kw)
        self.orient = orient
        self.command = command   # callable(first_fraction)
        if fg is None:
            fg = settings.c('sel')
        if bg is None:
            bg = settings.c('panel')
        if thumb is None:
            thumb = settings.c('sidebar_pin')
        self.fg = _color(fg)
        self.bg = _color(bg)
        self.thumb = _color(thumb)
        self._first = 0.0
        self._last = 1.0
        self._opts.update(orient=orient)
        self._recalc()

    def set(self, first, last):
        """The controlled widget reports the visible window (0..1)."""
        first = max(0.0, min(1.0, float(first)))
        last = max(first, min(1.0, float(last)))
        self._first, self._last = first, last

    def _recalc(self):
        if self.orient == 'vertical':
            self._natw = 12
            self._nath = 60
        else:
            self._natw = 60
            self._nath = 12
        self.w, self.h = self._natw, self._nath

    def _thumb_rect(self):
        if self.orient == 'vertical':
            total = max(self.h - 4, 1)
            span = (self._last - self._first) * total
            span = max(span, 8)
            y = int(self.y + 2 + self._first * total)
            y = min(y, self.y + self.h - 2 - span)
            return (self.x + 2, y, max(self.w - 4, 2), int(span))
        total = max(self.w - 4, 1)
        span = (self._last - self._first) * total
        span = max(span, 8)
        x = int(self.x + 2 + self._first * total)
        x = min(x, self.x + self.w - 2 - span)
        return (x, self.y + 2, int(span), max(self.h - 4, 2))

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        rx, ry, rw, rh = self._thumb_rect()
        fb.fill_rect(rx, ry, rw, rh, *self.thumb)
        fb.draw_rect(ox, oy, self.w, self.h, *(90, 100, 115))

    def on_click(self, x, y, button, dbl, clicks=1):
        if button != 1 or self.command is None:
            return
        if self.orient == 'vertical':
            total = max(self.h - 4, 1)
            rx, ry, rw, rh = self._thumb_rect()
            if y < ry:
                frac = max(0.0, self._first - (self._last - self._first))
            elif y >= ry + rh:
                frac = min(1.0, self._last + (self._last - self._first))
            else:
                frac = (y - self.y - 2 - rh / 2) / total
        else:
            total = max(self.w - 4, 1)
            rx, ry, rw, rh = self._thumb_rect()
            if x < rx:
                frac = max(0.0, self._first - (self._last - self._first))
            elif x >= rx + rw:
                frac = min(1.0, self._last + (self._last - self._first))
            else:
                frac = (x - self.x - 2 - rw / 2) / total
        frac = max(0.0, min(1.0, frac))
        try:
            self.command(frac)
        except Exception as e:
            kern.write('tk: scrollbar command error: %s\n' % e)


class _ShadowFB(Framebuffer):
    """Offscreen drawing surface: identical interface to Framebuffer but
    backed by a bytearray instead of VRAM.  Standalone tk roots render the
    whole widget tree into one of these and blit it to the screen once, so
    the display only ever shows complete frames -- no clear/repaint flash.
    """

    def __init__(self, src):
        # deliberately skip Framebuffer.__init__ (it reads kern.fb_info);
        # copy the source's geometry and drawing state instead
        self.width = src.width
        self.height = src.height
        self.stride = src.stride
        self.bpp = src.bpp
        self.format = src.format
        self.mem = bytearray(src.width * src.height * src.bpp)
        self.mirror_x = src.mirror_x
        self.mirror_glyphs = src.mirror_glyphs
        self._ch = src._ch
        self._phys_w = getattr(src, '_phys_w', src.width)
        self._phys_h = getattr(src, '_phys_h', src.height)
        self._origin_x = getattr(src, '_origin_x', 0)
        self._origin_y = getattr(src, '_origin_y', 0)


class Tk:
    """The widget root: owns the framebuffer, keyboard, layout and focus."""

    def __init__(self, title='Ripos', bg=(22, 26, 34), show_title=True, **kw):
        self._title = title
        self.bg = _color(bg)
        self.show_title = show_title
        self._opts = {}
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
        self._geom = None
        self._protocols = {}
        self._withdrawn = False
        self._shadow = None   # cached offscreen buffer (double-buffering)
        self._dirty = False   # set when a redraw is needed without one yet
        self._no_shadow = False  # True once the offscreen buffer fails to
                                 # allocate: stick to direct drawing instead
                                 # of retrying (and failing) every redraw
        self._mouse_hook = None  # bound on_mouse registered on the Mouse;  
                                 # removed on destroy so handlers don't pile
                                 # up across app switches
        # widget-less roots (image viewer, media player, ...): a raw mouse
        # handler that receives press/drag/wheel events the widgets never
        # see, so those apps can pan/zoom without a widget tree
        self._mouse_any = None
        self._press_any = False
        # app window system (appbar): sidebar strip, title-bar chrome,
        # minimize/close, drag tracking
        self.sidebar = bool(kw.get('sidebar', False))
        self._chrome = {}          # title-bar button rects from the last draw
        self._app_id = None        # appbar registration id
        self._minimized = False
        self._prev_kb = None       # the caller keyboard (restored on hide/quit)
        self._back = None          # callback that redraws the previous owner
        self._close_handler = None # set by run() wrappers: the full quit path
        self._press_widget = None  # widget that received the last left press
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
            # keep the bound handler so destroy() can remove it again
            self._mouse_hook = self.on_mouse
            mouse.on_event(self._mouse_hook)
        # Handing the screen to a new app: release the old root's offscreen
        # buffer so only one full-screen shadow exists at a time (the C heap
        # is small; a second 1280x1024x3 buffer can push it over the edge).
        if _ACTIVE_ROOT is not None and _ACTIVE_ROOT is not self:
            _ACTIVE_ROOT._shadow = None
        _ACTIVE_ROOT = self
        # Register in the app window system so the title-bar '-' (minimize)
        # and 'X' (close) buttons and the left sidebar work for this app.
        if not self._embedded and self._app_id is None:
            self._app_id = appbar.register(self._title, self._restore_app,
                                           self._close_app)
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

    # ---- app window system (title-bar chrome + sidebar) --------------

    def _minimize(self):
        """Title-bar '-': hide this app into the sidebar.  It keeps running;
        clicking its sidebar entry restores it.  The previous owner of the
        screen (the shell or the app that launched us) is redrawn."""
        global _ACTIVE_ROOT
        if self._minimized or self.destroyed or self._app_id is None:
            return
        self._minimized = True
        appbar.hide(self._app_id)
        if self._prev_kb is not None:
            self._prev_kb.activate()
        elif self.kb is not None:
            try:
                import keyboard as _kbd
                if _kbd._CURRENT is self.kb:
                    _kbd.restore_base()
            except Exception:
                pass
        _ACTIVE_ROOT = None
        self._shadow = None   # free the offscreen buffer while hidden
        if self._back is not None:
            try:
                self._back()
            except Exception as e:
                kern.write('tk: minimize back() failed: %s\n' % e)

    def _restore_app(self):
        """Sidebar entry clicked: bring the minimized app back."""
        if self.destroyed:
            return
        self._minimized = False
        appbar.unhide(self._app_id)
        if self.kb is not None:
            self.kb.activate()
        self.activate()

    def _close_app(self):
        """Title-bar 'X': close the app for good (its run() quit path)."""
        if self._close_handler is not None:
            self._close_handler()
        else:
            self.destroy()

    def _open_pinned(self, index):
        """Sidebar pinned-folder entry clicked.  If this app knows how to
        navigate (fm sets _pinned_handler), it navigates; otherwise the file
        manager is launched at that folder (and returns here on quit)."""
        try:
            path = appbar.PINNED[index]
        except (IndexError, TypeError):
            return
        if getattr(self, '_pinned_handler', None) is not None:
            try:
                self._pinned_handler(path)
            except Exception as e:
                kern.write('tk: pinned nav failed: %s\n' % e)
            return
        try:
            import fm

            def back():
                self.activate()

            fm.run(self.fb, self.kb, self.mouse, on_quit=back, path=path)
        except Exception as e:
            kern.write('tk: pinned open failed: %s\n' % e)

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
        if getattr(self, '_building', False):
            self._pending_keys.append(ev)
            return
        name, ch, pressed = ev
        if not pressed:
            return
        # M9.6 hotkey pre-pass: Ctrl+letter fires root-level <Control-x>
        # binds (clipboard copy/paste/cut, editor undo, ...) BEFORE any
        # widget routing.
        if ch and self.kb is not None and self.kb.ctrl_down():
            c = chr(ch).lower()
            if 97 <= ord(c) <= 122:
                seq = "<Control-%s>" % c
                if self._fire_bind(None, seq, c):
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
        """Mouse event (x, y, button, pressed, dbl, clicks, wheel) from
        mouse.Mouse.

        Handles the app-window chrome (title-bar '-'/'X'), the left
        sidebar, the mouse wheel, and press/drag/release -- and only
        repaints if this root still owns the screen (a click handler may
        have launched or quit another app, which takes over the display).
        """
        if self.destroyed:
            return
        if _ACTIVE_ROOT is not self:
            return  # another app owns the screen
        x, y, button, pressed, dbl, clicks, wheel = ev

        # 3. mouse wheel scrolls the widget under the cursor (or focused);
        #    move events (button 0, not pressed) never trigger actions.
        #    Widget-less roots (viewers/players) hook _mouse_any and get the
        #    wheel when nothing scrollable is under the cursor.
        if wheel:
            target = self._hit_test(x, y) or self._focused()
            if target is not None and target.on_wheel(wheel):
                self.redraw()
            elif self._mouse_any is not None:
                self._mouse_any(ev)
            return

        # 4. left button: press / drag / release
        if button == 1:
            if pressed:
                # 1. title-bar chrome: '-' (minimize) and 'X' (close) --
                #    only on a real press, NEVER on hover/move
                if not self._embedded:
                    ch = appbar.hit_chrome(x, y, self._chrome)
                    if ch == 'min':
                        self._minimize()
                        return
                    if ch == 'close':
                        self._close_app()
                        return

                # 2. left sidebar (pinned folders / hidden apps)
                if self.sidebar and x < appbar.SIDEBAR_W:
                    act = appbar.hit_sidebar(x, y, self.fb.height,
                                             y0=wm.TITLE_H)
                    if act is not None:
                        kind, arg = act
                        if kind == 'pin':
                            self._open_pinned(arg)
                        elif kind == 'app':
                            appbar.restore(arg)
                        return
                # 2b. right sidebar (configurable: pinned / apps)
                if self.sidebar:
                    act = appbar.hit_sidebar_right(x, y, self.fb.width,
                                                   self.fb.height,
                                                   y0=wm.TITLE_H)
                    if act is not None:
                        kind, arg = act
                        if kind == 'pin':
                            self._open_pinned(arg)
                        elif kind == 'app':
                            appbar.restore(arg)
                        return

                target = self._hit_test(x, y)
                self._press_widget = target
                if target is None and self._mouse_any is not None:
                    # widget-less root: hand the whole gesture to it
                    self._press_any = True
                    self._mouse_any(ev)
                    return
                if target is not None:
                    if target.focusable:
                        fs = self._focusables()
                        if target in fs:
                            self._focus_idx = fs.index(target)
                    target.on_click(x, y, button, dbl, clicks)
                if _ACTIVE_ROOT is self:
                    self.redraw()
            else:
                if self._press_any:
                    self._press_any = False
                    if self._mouse_any is not None:
                        self._mouse_any(ev)
                    return
                t = self._press_widget
                self._press_widget = None
                if t is not None:
                    t.on_release(x, y)
                if _ACTIVE_ROOT is self:
                    self.redraw()
            return
        if button == 0 and not pressed and (self._press_widget is not None
                                            or self._press_any):
            # drag while the left button is held
            if self._press_any and self._mouse_any is not None:
                self._mouse_any(ev)
            else:
                self._press_widget.on_drag(x, y)
            if _ACTIVE_ROOT is self:
                self.redraw()
            return
        if button in (2, 4) and pressed:
            # right / middle click: route like a press (widgets decide)
            target = self._hit_test(x, y)
            if target is not None:
                if target.focusable:
                    fs = self._focusables()
                    if target in fs:
                        self._focus_idx = fs.index(target)
                target.on_click(x, y, button, dbl, clicks)
                if _ACTIVE_ROOT is self:
                    self.redraw()
            return

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
        if getattr(self, '_building', False):
            # app construction: every pack() triggers a redraw; defer them
            # all to the single end_build() repaint so startup is fast and
            # keys typed immediately after launching are not lost
            self._dirty = True
            return
        self._dirty = False
        self.draw_region(self.fb, 0, 0, self.fb.width, self.fb.height)

    def begin_build(self):
        """Fast app construction: defer redraws and buffer keys until
        end_build()."""
        self._building = True
        self._pending_keys = []

    def end_build(self):
        """Finish app construction: one repaint, then replay any keys that
        arrived while the tree was being built."""
        self._building = False
        self.redraw()
        if getattr(self, '_pending_keys', None):
            for ev in self._pending_keys:
                self.on_key(ev)
            self._pending_keys = []

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
        """Draw the whole tree into (ox, oy)-(ox+rw, oy+rh) of fb.

        Standalone roots (the window is the whole screen) render into an
        offscreen buffer and blit it to VRAM in one go, so the user never
        sees the intermediate clear/repaint.  Embedded roots (hosted in a
        wm window) keep drawing directly, because they share the desktop's
        framebuffer with the other windows.

        Subclasses may implement their own draw_region (e.g. a viewer that
        blits an image instead of widgets); they should call
        _begin_shadow()/_end_shadow() and _draw_app_frame() so the app
        chrome (title-bar -/X, sidebars) and the double-buffer behave the
        same way as the widget tree path.
        """
        if self._minimized:
            return  # hidden in the sidebar: nothing to paint
        buffered, target = self._begin_shadow(fb, ox, oy, rw, rh)
        sx, sxr, y = self._draw_app_frame(target, ox, oy, rw, rh)
        self._mark_focus()
        cavity = (ox + sx, y, rw - sx - sxr, rh - (y - oy))
        placements = _layout(self, cavity, (ox + sx, oy), self.children)
        for w in self.children:
            if w in placements:
                px, py, pw, ph = placements[w]
                w.x, w.y, w.w, w.h = px, py, pw, ph
                w.draw(target, px, py)
        self._end_shadow(fb, target, buffered)

    def _begin_shadow(self, fb, ox, oy, rw, rh):
        """Standalone full-screen roots render into an offscreen buffer and
        blit it to VRAM in one atomic copy, so the user never sees the
        intermediate clear/repaint.  Returns (buffered, target) where
        target is the buffer to paint into (or fb when drawing directly)."""
        if self._minimized:
            return False, None
        buffered = (not self._embedded and self.fb is fb
                    and ox == 0 and oy == 0 and rw == fb.width
                    and rh == fb.height)
        target = fb
        if buffered and not self._no_shadow:
            bb = self._shadow
            if (bb is None or bb.width != fb.width or bb.height != fb.height
                    or bb.bpp != fb.bpp or bb.stride != fb.stride):
                if bb is not None:
                    bb = None  # size changed: drop the stale buffer
                try:
                    # Reclaim any collectable garbage first (e.g. a released
                    # shadow from a previous app) so the big allocation fits.
                    import gc
                    gc.collect()
                    bb = _ShadowFB(fb)
                except MemoryError:
                    # The C heap is tight; degrade to direct drawing rather
                    # than crashing the app -- and REMEMBER it, so we don't
                    # retry the expensive allocation (and re-flash) on every
                    # single redraw.  Direct drawing clears + repaints into
                    # VRAM, so the user sees a flicker until the heap frees.
                    self._no_shadow = True
                    bb = None
                self._shadow = bb
            if bb is None:
                buffered = False
                target = fb
            else:
                target = bb
        return buffered, target

    def _draw_app_frame(self, target, ox, oy, rw, rh):
        """Paint the app window system frame: left/right sidebars, the
        theme background and the title bar with -/X chrome.  Returns
        (sx, sxr, y): the sidebars' widths and the y below the title bar
        where content starts."""
        sx = 0
        sxr = 0
        if self.sidebar and not self._embedded:
            appbar.draw_sidebar(target, y0=wm.TITLE_H)
            sx = appbar.SIDEBAR_W
            appbar.draw_sidebar_right(target, y0=wm.TITLE_H)
            sxr = appbar.right_sidebar_w()
        t = settings.theme()
        target.fill_rect(ox + sx, oy, rw - sx - sxr, rh, *t['bg'])
        y = oy
        if self.show_title:
            target.fill_rect(ox + sx, y, rw - sx - sxr, wm.TITLE_H,
                             *t['title_bg'])
            textmod.draw_text(target, ox + sx + 4, y + 2, self._title,
                              t['title_fg'])
            if not self._embedded:
                self._chrome = appbar.draw_chrome(target, ox + sx, y,
                                                  rw - sx - sxr)
            y += wm.TITLE_H
        return sx, sxr, y

    def _end_shadow(self, fb, target, buffered):
        """Finish a draw_frame: copy the offscreen buffer to VRAM in one go
        and paint the cursor on top of the visible screen."""
        if buffered:
            # one atomic frame copy to the visible framebuffer.  Row-wise
            # slice assignment: a full [:] = bytearray fails on the
            # kernel's zero-copy memoryview ("different structures"), and
            # these partial slices are the proven-working write path.
            m = fb.mem
            rowb = target.width * target.bpp
            tmem = target.mem
            for yy in range(target.height):
                o = target._off(0, yy)
                m[o:o + rowb] = tmem[o:o + rowb]
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
        # Only repaint when something marked the root dirty without going
        # through redraw() itself; every event-driven redraw already paints
        # the current frame, so the timer must not blink the screen.
        if self._dirty:
            self._dirty = False
            self.redraw()
        kern.after(1000, self._tick)

    def destroy(self):
        global _ACTIVE_ROOT
        global _LAST_ROOT
        if self.destroyed:
            return
        cb = self._protocols.get('WM_DELETE_WINDOW')
        if cb is not None:
            try:
                cb()
            except Exception as e:
                kern.write('tk: WM_DELETE_WINDOW handler error: %s\n' % e)
        # Drop this root's mouse hook so Mouse.handlers does not keep the
        # whole widget tree alive after the app quits (a leak that grows
        # with every app switch and eventually starves the C heap).
        if self.mouse is not None and self._mouse_hook is not None:
            try:
                if self._mouse_hook in self.mouse.handlers:
                    self.mouse.handlers.remove(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None
        # The screen now belongs to whoever draws next (the shell, or the
        # app that launched us).  Clear the ownership so a stale handler
        # (e.g. the click that pressed this app's Quit button) cannot
        # repaint a dead root over the new owner.
        if _ACTIVE_ROOT is self:
            _ACTIVE_ROOT = None
        # _LAST_ROOT pins the most recent Tk root for tkinter.mainloop(); a
        # destroyed root must not stay referenced or the whole widget tree
        # (and any big payloads, e.g. the media player's decoded PCM) leaks
        # until the next root is created -- enough to starve the C heap.
        if _LAST_ROOT is self:
            _LAST_ROOT = None
        # The appbar registry holds bound restore/close methods that pin
        # this app object; drop it or the app (and its payloads, e.g. the
        # media player's decoded PCM) leaks until the next app reuses it.
        if self._app_id is not None:
            try:
                import appbar as _appbar
                _appbar.unregister(self._app_id)
            except Exception:
                pass
            self._app_id = None
        # If this app's own keyboard is still the kernel hook (the app
        # quit without restoring -- e.g. 'import tkdemo' + its Quit
        # button), hand the keyboard back to the shell so it is not stuck.
        if self.kb is not None:
            try:
                import keyboard as _kbd
                if _kbd._CURRENT is self.kb:
                    _kbd.restore_base()
                # Break the root <-> keyboard <-> handler cycle: the Keyboard
                # holds this root's bound on_key, so without this the whole
                # app (and big payloads such as the media player's decoded
                # PCM) stays alive until a cyclic GC happens to run.
                self.kb.handlers = [
                    h for h in getattr(self.kb, 'handlers', [])
                    if getattr(h, '__self__', None) is not self
                ]
            except Exception:
                pass
            self.kb = None
        # An app that quits with no back() callback of its own leaves the
        # screen empty: let the shell repaint immediately.
        if self._back is None and appbar._fallback_redraw is not None:
            try:
                appbar._fallback_redraw()
            except Exception:
                pass
        self.destroyed = True
        self._shadow = None   # release the offscreen buffer
        # Drop self-referential callbacks (root -> _quit/_close_handler) so
        # the object is refcount-collectible the moment the quit path
        # returns; a deferred GC then sweeps any residual widget-tree cycles
        # (root <-> children, keyboard handlers) that would otherwise pin
        # big payloads (e.g. the media player's ~20 MB decoded PCM) and
        # starve the C heap over repeated app switches.
        self._close_handler = None
        try:
            self._protocols.clear()
        except Exception:
            pass
        try:
            import gc
            kern.after(10, gc.collect)
        except Exception:
            pass
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
