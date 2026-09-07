"""tkinter -- a tkinter-compatible module for the Ripos OS (pure Python).

This is what `import tkinter` resolves to on Ripos: the stdlib tkinter
package (which needs the _tkinter C extension and a Tcl/Tk runtime) is
excluded from the initramfs, so this package stands in for it.  It re-exports
the widgets from the core `tk` toolkit and adds the tkinter API surface --
module-level mainloop(), the standard constants, variables, and the font and
messagebox submodules -- so that unmodified tkinter programs run on the
framebuffer.

There is no Tcl/Tk and no X server: widgets draw through the framebuffer
driver, the PS/2 keyboard is the only input device, and mainloop() is
non-blocking (the kernel event loop drives the UI).  See docs/TKINTER.md for
what is supported and what is stubbed.
"""

import kern
import tk as _tk
from tk import (Tk, Widget, Label, Button, Canvas, Frame, Entry,
                Listbox, StringVar, IntVar, DoubleVar, BooleanVar,
                WidgetWindow, _color)
# NOTE: app composition (handing the screen between roots) uses the
# core module's tk._LAST_ROOT / tk._ACTIVE_ROOT, which are live globals.

# ---------------------------------------------------------------------------
# version + constants (the ones real tkinter programs actually use)

TkVersion = 8.6
TkinterVersion = '8.6'

END = 'end'
INSERT = 'insert'
CURRENT = 'current'
ALL = 'all'

LEFT, RIGHT, TOP, BOTTOM = 'left', 'right', 'top', 'bottom'
CENTER, N, S, E, W = 'center', 'n', 's', 'e', 'w'
NW, NE, SW, SE = 'nw', 'ne', 'sw', 'se'
X, Y, BOTH, NONE = 'x', 'y', 'both', 'none'
HORIZONTAL, VERTICAL = 'horizontal', 'vertical'

NORMAL, ACTIVE, DISABLED = 'normal', 'active', 'disabled'
RAISED, SUNKEN, FLAT = 'raised', 'sunken', 'flat'
RIDGE, GROOVE, SOLID = 'ridge', 'groove', 'solid'

TRUE, FALSE, YES, NO = True, False, True, False
OK, CANCEL = 'ok', 'cancel'
RETRY, IGNORE = 'retry', 'ignore'
ABORT = 'abort'


class Toplevel(Tk):
    """A second window.  Ripos has a single screen, so Toplevel is the same
    as Tk: it takes over the framebuffer.  A warning is written to serial."""

    def __init__(self, master=None, **kw):
        kern.write('tkinter: Toplevel mapped to the single Ripos screen\n')
        Tk.__init__(self, **kw)


def mainloop(n=0):
    """Run the most recently created Tk root's mainloop (non-blocking)."""
    root = _tk._LAST_ROOT
    if root is None:
        kern.write('tkinter: mainloop called with no Tk() root\n')
        return
    root.mainloop(n)


__all__ = [
    'Tk', 'Widget', 'Label', 'Button', 'Canvas', 'Frame', 'Entry',
    'Listbox', 'StringVar', 'IntVar', 'DoubleVar', 'BooleanVar', 'Toplevel',
    'mainloop',
    'TkVersion', 'TkinterVersion',
    'END', 'INSERT', 'CURRENT', 'ALL',
    'LEFT', 'RIGHT', 'TOP', 'BOTTOM', 'CENTER', 'N', 'S', 'E', 'W',
    'NW', 'NE', 'SW', 'SE', 'X', 'Y', 'BOTH', 'NONE',
    'HORIZONTAL', 'VERTICAL', 'NORMAL', 'ACTIVE', 'DISABLED',
    'RAISED', 'SUNKEN', 'FLAT', 'RIDGE', 'GROOVE', 'SOLID',
    'TRUE', 'FALSE', 'YES', 'NO', 'OK', 'CANCEL', 'RETRY', 'IGNORE', 'ABORT',
]
