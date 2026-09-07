"""tkinter.font -- a minimal Font object for the Ripos tkinter module.

Ripos has one fixed bitmap font (text.py: ASCII 8x8 at 2x + 16x16 CJK), so
Font ignores family/size except for reporting.  The API surface -- Font(),
measure(), metrics(), actual(), cget(), families(), copy() -- matches
tkinter.font so code that measures strings or inspects fonts keeps working.
"""

import text as _text
from tk import _color  # noqa: F401  (kept for API parity with font modules)


class Font:
    def __init__(self, root=None, font=None, name=None, exists=False,
                 **options):
        self.name = name
        self._options = dict(options)
        if isinstance(font, dict):
            self._options.update(font)
        self._options.setdefault('family', 'TkFixedFont')
        self._options.setdefault('size', -16)

    def measure(self, text):
        """Width of text in pixels at the Ripos fixed font."""
        return _text.text_width(text)

    def metrics(self, options=None):
        m = {'ascent': _text.LINE_H - 4, 'descent': 4,
             'linespace': _text.LINE_H, 'fixed': True}
        if options is None:
            return m
        return m.get(options)

    def families(self, root=None):
        return ('monospace', 'fixed', 'TkFixedFont')

    def actual(self, option=None, **kw):
        if option is not None:
            return self._options.get(option)
        return dict(self._options)

    def cget(self, option):
        return self._options.get(option)

    def configure(self, **kw):
        self._options.update(kw)

    def copy(self):
        return Font(**self._options)

    def __str__(self):
        return self.name or 'font %s' % (self._options,)

    def __repr__(self):
        return '<tkinter.font.Font %s>' % (self._options,)

    def __eq__(self, other):
        return isinstance(other, Font) and self._options == other._options

    def __hash__(self):
        return hash(tuple(sorted(self._options.items())))
