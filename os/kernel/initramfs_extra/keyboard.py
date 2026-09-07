"""keyboard.py -- M6 keyboard event driver (pure Python).

The kernel decodes PS/2 scancodes and hands (name, char, pressed) events to
one registered callback (kern.on_key).  This driver turns that single kernel
hook into a real driver: it fans events out to any number of Python handlers
and keeps a queue for poll()/read() style access.
"""

import kern

# Who currently owns kern.on_key (the single kernel hook).  Every Keyboard
# created becomes _CURRENT; apps hand the hook around via activate() /
# restore_base() so the SHELL (or the launching app) gets the keyboard back
# even when an app quits without restoring it itself (e.g. `import tkdemo`
# then clicking its Quit button).
_CURRENT = None
_BASE = None    # the shell's keyboard, set by boot.py


class Keyboard:
    """Event driver over kern.on_key."""

    def __init__(self):
        global _CURRENT
        self.handlers = []   # Python callbacks, called with (name, ch, pressed)
        self.queue = []      # every event, for poll()/read()
        self.pressed = {}    # name -> True while the key is held
        kern.on_key(self._dispatch)
        _CURRENT = self

    def activate(self):
        """Make this keyboard the kernel hook again (and record it)."""
        global _CURRENT
        kern.on_key(self._dispatch)
        _CURRENT = self

    def _dispatch(self, ev):
        name, ch, pressed = ev
        self.queue.append(ev)
        self.pressed[name] = bool(pressed)
        for handler in self.handlers:
            handler(ev)

    def on_event(self, handler):
        """Register a callback; it receives every event as (name, ch, pressed)."""
        self.handlers.append(handler)

    def poll(self):
        """Return and clear all pending events."""
        q = self.queue
        self.queue = []
        return q

    def read(self):
        """Block until the next event (releases the GIL while waiting)."""
        while not self.queue:
            kern.sleep(2)
        return self.queue.pop(0)

    def is_down(self, name):
        return self.pressed.get(name, False)

    def ctrl_down(self):
        """True while the Ctrl key is held (kernel emits name='ctrl')."""
        return self.pressed.get("ctrl", False)

    def shift_down(self):
        return self.pressed.get("shift", False) or self.pressed.get("right_shift", False)

    def alt_down(self):
        """True while the Alt key is held (kernel emits name='alt' for
        both the left and the E0-extended right Alt key)."""
        return self.pressed.get("alt", False)


def restore_base():
    """Give the keyboard back to the shell (the base keyboard)."""
    global _CURRENT
    if _BASE is not None:
        kern.on_key(_BASE._dispatch)
        _CURRENT = _BASE