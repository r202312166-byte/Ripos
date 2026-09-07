"""repl.py -- M8: the interactive Python REPL that is the Ripos command line.

The shell reads the keyboard (kern.on_key events), echoes each submitted line
to the serial console and the framebuffer, and evaluates it with kern.eval --
a persistent __main__-style namespace.  Bare expressions print their value,
exceptions come back as a one-line message, and `exit` powers the machine off.

This is the whole point of the project: the kernel only supplies the
interpreter, the framebuffer and the keyboard; the OS command line is
Python, written in Python.
"""

import sys
import kern
import text
import appbar
import settings

PROMPT = '>>> '
BANNER = 'Ripos shell -- Python 3.12.10 (help / exit)'
HELP = [
    'Ripos shell commands:',
    '  help                show this help',
    '  clear               clear the screen',
    '  mirror [off|glyphs|full]  display orientation (bare mirror cycles)',
    '  res [WxH]           change the display resolution (e.g. res 800x600)',
    '  apps                list the apps in /apps',
    '  config              open the settings app (theme, language, sidebars)',
    '  fm                  open the GUI file manager',
    '  edit [path]         open the text editor (ISE-style; F5 runs the file)',
    '  imgview [path]      open the image viewer (PNG/JPEG; default demo)',
    '  imgedit [savepath]  open the pixel paint editor (saves a PNG)',
    '  media [path]        open the media player (MP3 stream / MP4 track)',
    '  browse [url]        open the web browser (plain HTTP; default demo page)',
    '  arc [path]          open an archive viewer (zip/7z/tar/gz/bz2/xz)',
    '  tkdemo|tkhello|tkform|tkwindow   run a widget demo',
    '  exit, quit          power the machine off',
    '',
    'Everything else is Python.  Examples:',
    "  1 + 1",
    '  import kern',
    '  kern.tick()',
    '  [i * i for i in range(6)]',
]

# colors come from the active theme (settings.apply_theme() re-colors a
# running shell on switch)
_T = settings.theme()
BG = _T['bg']
TITLE = _T['prompt']
PROMPT_COLOR = _T['prompt']
TEXT_COLOR = _T['fg']
ERROR_COLOR = _T['err']
CURSOR = _T['cursor']


class _ShellWriter:
    """A file-like object that captures print()/stderr output during a
    kern.eval so it shows up in the shell scrollback.  With echo=True it
    ALSO writes straight to the serial console, preserving the serial
    transcript order that the M8 gate reads."""

    def __init__(self, sink, echo=True):
        self.sink = sink
        self.echo = echo

    def write(self, s):
        s = str(s)
        self.sink.append(s)
        if self.echo:
            kern.write(s)

    def flush(self):
        pass


class Shell:
    """A full-screen REPL: output scrollback on top, input line at the bottom."""

    def __init__(self, fb, kb, mouse=None):
        self.fb = fb
        self.kb = kb
        self.mouse = mouse       # optional mouse.Mouse (cursor overlay)
        self.line = ''
        self.pos = 0             # cursor position within the input line
        self.lines = []          # scrollback as (text, color) tuples
        self.history = []
        self.hist = -1           # history browse index; -1 = editing fresh line
        self.scroll = 0          # scrollback offset (lines scrolled back)
        self.max_out = max((fb.height - 3 * text.LINE_H) // text.LINE_H, 1)
        if mouse is not None:
            mouse.on_event(self.on_mouse)   # sidebar clicks
        self._draw()
        kb.on_event(self.on_key)

    # ---- mouse (app window system sidebar) ---------------------------

    def on_mouse(self, ev):
        x, y, button, pressed, dbl, clicks, wheel = ev
        if button == 1 and pressed:
            # an app owns the screen: its own handler does the sidebar /
            # chrome work; the shell must not also act on the click
            try:
                import tk as tkmod
                if tkmod._ACTIVE_ROOT is not None:
                    return
            except Exception:
                pass
            # right-edge scrollback scrollbar
            if self._scrollbar_hit(x, y):
                return
            act = appbar.hit_sidebar(x, y, self.fb.height, y0=0)
            if act is not None:
                kind, arg = act
                if kind == 'pin':
                    self._open_pinned(arg)
                elif kind == 'app':
                    appbar.restore(arg)
                return
            act = appbar.hit_sidebar_right(x, y, self.fb.width,
                                           self.fb.height, y0=0)
            if act is not None:
                kind, arg = act
                if kind == 'pin':
                    self._open_pinned(arg)
                elif kind == 'app':
                    appbar.restore(arg)

    def _open_pinned(self, index):
        try:
            path = appbar.PINNED[index]
        except (IndexError, TypeError):
            return

        def on_quit():
            self.refresh()

        import fm
        fm.run(self.fb, self.kb, self.mouse, on_quit=on_quit, path=path)

    def refresh(self):
        """Redraw the shell (used when an app like the file manager hands
        the screen back)."""
        self._draw()
        kern.write('shell: ready\n')

    # ---- keyboard ----------------------------------------------------

    def on_key(self, ev):
        name, ch, pressed = ev
        if not pressed:
            return
        if name == 'enter':
            self._submit()
        elif name == 'backspace':
            if self.pos > 0:
                self.line = self.line[:self.pos - 1] + self.line[self.pos:]
                self.pos -= 1
                self._redraw_input()
        elif name == 'left':
            if self.pos > 0:
                self.pos -= 1
                self._redraw_input()
        elif name == 'right':
            if self.pos < len(self.line):
                self.pos += 1
                self._redraw_input()
        elif name == 'home':
            if self.pos != 0:
                self.pos = 0
                self._redraw_input()
        elif name == 'end':
            if self.pos != len(self.line):
                self.pos = len(self.line)
                self._redraw_input()
        elif name == 'up':
            self._browse_history(-1)
        elif name == 'down':
            self._browse_history(1)
        elif name == 'pgup':
            if self.scroll < self._max_scroll():
                self.scroll = min(self._max_scroll(),
                                  self.scroll + self.max_out)
                self._draw()
        elif name == 'pgdn':
            if self.scroll > 0:
                self.scroll = max(0, self.scroll - self.max_out)
                self._draw()
        elif ch is not None and 32 <= ch < 127:
            self.line = self.line[:self.pos] + chr(ch) + self.line[self.pos:]
            self.pos += 1
            self._redraw_input()

    def _browse_history(self, step):
        n = len(self.history)
        if n == 0:
            return
        if step < 0:                        # up: older
            self.hist = n - 1 if self.hist == -1 else max(0, self.hist - 1)
        else:                               # down: newer
            if self.hist == -1:
                return
            self.hist -= 1
            if self.hist < -1:
                self.hist = -1
        self.line = '' if self.hist == -1 else self.history[self.hist]
        self.pos = len(self.line)
        self._redraw_input()

    def _submit(self):
        line = self.line
        self.line = ''
        self.pos = 0
        self.hist = -1
        stripped = line.strip()
        kern.write('%s%s\n' % (PROMPT, line))
        if stripped in ('exit', 'quit'):
            self._out('Ripos: powering off', ERROR_COLOR)
            kern.write('Ripos: powering off\n')
            kern.poweroff()
            return
        if stripped == 'help':
            for h in HELP:
                kern.write(h + '\n')
                self._out(h, TEXT_COLOR)
            self._draw()
            return
        if stripped == 'clear':
            self.lines = []
            self._draw()
            return
        if stripped == 'res' or stripped.startswith('res '):
            arg = stripped[len('res'):].strip()
            if not arg:
                self._out('display: %dx%d (physical %dx%d)'
                          % (self.fb.width, self.fb.height,
                             self.fb._phys_w, self.fb._phys_h), TEXT_COLOR)
            else:
                try:
                    w, h = arg.lower().split('x')
                    w, h = int(w), int(h)
                except (ValueError, TypeError):
                    self._out("res: usage 'res 800x600'", ERROR_COLOR)
                    kern.write("res: usage 'res 800x600'\n")
                else:
                    nw, nh, real = self.fb.set_resolution(w, h)
                    if self.mouse is not None:
                        self.mouse.set_fb(self.fb)
                    self._out('display: %dx%d (%s)' % (nw, nh,
                              'hardware' if real else 'virtual'), TEXT_COLOR)
                    kern.write('res: %dx%d real=%s\n' % (nw, nh, real))
            self._draw()
            return
        if stripped == 'apps':
            try:
                import os as _os
                names = sorted(_os.listdir('/apps'))
            except Exception as e:
                names = ['(cannot list /apps: %s)' % e]
            for n in names:
                kern.write('/apps/%s\n' % n)
                self._out('/apps/' + n, TEXT_COLOR)
            self._draw()
            return
        if stripped == 'config':
            kern.write('shell: starting settings app\n')
            self._run_app('config')
            return
        if stripped in ('tkdemo', 'tkhello', 'tkform', 'tkwindow'):
            kern.write('shell: running %s\n' % stripped)
            self._run_app(stripped)
            return
        if stripped == 'fm':
            kern.write('shell: starting file manager\n')
            self._run_app('fm')
            return
        if stripped == 'edit' or stripped.startswith('edit '):
            arg = stripped[len('edit'):].strip()
            kern.write('shell: starting editor (%s)\n' % (arg or '<new>'))
            self._run_app('editor', arg or None)
            return
        if stripped == 'imgview' or stripped.startswith('imgview '):
            arg = stripped[len('imgview'):].strip()
            kern.write('shell: starting imgview (%s)\n' % (arg or '<demo>'))
            self._run_app('imgview', arg or None)
            return
        if stripped == 'imgedit' or stripped.startswith('imgedit '):
            arg = stripped[len('imgedit'):].strip()
            kern.write('shell: starting imgedit (%s)\n' % (arg or '<new>'))
            self._run_app('imgedit', arg or None)
            return
        if stripped in ('media', 'mplayer') \
                or stripped.startswith('media ') \
                or stripped.startswith('mplayer '):
            arg = stripped[len('media'):].strip() if stripped.startswith('media') \
                else stripped[len('mplayer'):].strip()
            kern.write('shell: starting mplayer (%s)\n' % (arg or '<none>'))
            self._run_app('mplayer', arg or None)
            return
        if stripped == 'browse' or stripped.startswith('browse '):
            arg = stripped[len('browse'):].strip()
            kern.write('shell: starting browser (%s)\n' % (arg or '<default>'))
            self._run_app('browser', arg or None)
            return
        if stripped == 'arc' or stripped.startswith('arc '):
            arg = stripped[len('arc'):].strip()
            kern.write('shell: starting archive viewer (%s)\n' % (arg or '<none>'))
            self._run_app('arc', arg or None)
            return
        if stripped == 'editor' or stripped.startswith('editor '):
            arg = stripped[len('editor'):].strip()
            kern.write('shell: starting editor (%s)\n' % (arg or '<new>'))
            self._run_app('editor', arg or None)
            return
        if stripped == 'mirror' or stripped.startswith('mirror '):
            # live display-orientation selector, cycles without a rebuild:
            #   off     -- normal rendering (default)
            #   full    -- mirror the whole display left-right (MIRROR_X:
            #              for displays that render the framebuffer mirrored)
            #   glyphs  -- flip each glyph in place (MIRROR_GLYPHS: for
            #              displays that show inverted letters, 'p' -> 'q')
            # cycle order puts the most likely fix (glyphs) first
            MODES = ('off', 'glyphs', 'full')
            arg = stripped[len('mirror'):].strip()
            if arg:
                state = arg if arg in MODES else None
            else:
                cur = 'glyphs' if self.fb.mirror_glyphs else \
                      ('full' if self.fb.mirror_x else 'off')
                state = MODES[(MODES.index(cur) + 1) % len(MODES)]
            if state is None:
                self._out("mirror: unknown mode '%s' (off|full|glyphs)" % arg,
                          ERROR_COLOR)
                kern.write('mirror: unknown mode %s\n' % arg)
                self._draw()
                return
            self.fb.mirror_x = (state == 'full')
            self.fb.mirror_glyphs = (state == 'glyphs')
            self._out('display mirroring: %s' % state, TEXT_COLOR)
            kern.write('display mirroring: %s\n' % state)
            self._draw()
            return
        if stripped:
            self.history.append(line)
            captured = []
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = _ShellWriter(captured, echo=True)
            sys.stderr = _ShellWriter(captured, echo=True)
            try:
                result = kern.eval(line)
            finally:
                sys.stdout, sys.stderr = old_out, old_err
            for part in ''.join(captured).split('\n'):
                if part:
                    self._out(part, TEXT_COLOR)
            if result is not None:
                kind, text = result
                self._out(text, ERROR_COLOR if kind == 'err' else TEXT_COLOR)
        self.scroll = 0   # a fresh command returns to the newest view
        # If the command launched a GUI app (e.g. 'import tkdemo' or
        # 'import m7'), that app owns the screen now -- the shell must NOT
        # repaint over it.  Only redraw when no tk root is active.
        try:
            import tk as _tkmod
            if _tkmod._ACTIVE_ROOT is None:
                self._draw()
        except Exception:
            self._draw()

    def _run_app(self, name, path=None):
        """Launch a full-screen GUI app.  The app installs its own keyboard
        handler (kern.on_key is single-slot); when it quits it must restore
        this shell's keyboard and call back so we redraw."""
        def on_quit():
            self.refresh()
        if name == 'config':
            import config
            config.run(self.fb, self.kb, self.mouse, on_quit=on_quit)
        elif name in ('tkdemo', 'tkhello', 'tkform', 'tkwindow'):
            # module-level demo scripts run on import; re-running them from
            # the shell must re-execute the module (import caches it)
            import importlib
            mod = sys.modules.get(name)
            if mod is None:
                __import__(name)
            else:
                importlib.reload(mod)
        elif name == 'fm':
            import fm
            fm.run(self.fb, self.kb, self.mouse, on_quit=on_quit)
        elif name == 'imgview':
            import imgview
            imgview.run(self.fb, self.kb, self.mouse, on_quit=on_quit,
                        path=path)
        elif name == 'mplayer':
            import mplayer
            mplayer.run(self.fb, self.kb, self.mouse, on_quit=on_quit,
                        path=path)
        elif name == 'imgedit':
            import imgedit
            imgedit.run(self.fb, self.kb, self.mouse, on_quit=on_quit,
                        path=path)
        elif name == 'browser':
            import browser
            browser.run(self.fb, self.kb, self.mouse, on_quit=on_quit,
                        url=path)
        elif name == 'arc':
            import arc
            arc.run(self.fb, self.kb, self.mouse, on_quit=on_quit,
                    path=path)
        else:
            import editor
            editor.run(self.fb, self.kb, self.mouse, on_quit=on_quit,
                       path=path)
        # mainloop() returns immediately; the app draws itself and the
        # kernel loop drives it.  The shell is redrawn by on_quit.

    # ---- rendering ---------------------------------------------------

    def _out(self, s, color):
        for part in str(s).split('\n'):
            self.lines.append((part, color))
        if len(self.lines) > 2000:
            self.lines = self.lines[-1000:]

    def _max_scroll(self):
        return max(0, len(self.lines) - self.max_out)

    def _content_x(self):
        return appbar.SIDEBAR_W + 8

    def _content_w(self):
        """Usable width between the left and right sidebars."""
        w = self.fb.width - appbar.SIDEBAR_W - appbar.right_sidebar_w()
        return max(w, 120)

    def _draw(self):
        fb = self.fb
        fb.clear(*BG)
        appbar.draw_sidebar(fb)          # pinned folders + hidden apps
        appbar.draw_sidebar_right(fb)    # optional right sidebar
        x0 = self._content_x()
        text.draw_text(fb, x0, 6, BANNER, TITLE)
        # scrollback view window (self.scroll lines back from the newest)
        total = len(self.lines)
        end = total - self.scroll
        start = max(0, end - self.max_out)
        tail = self.lines[start:end]
        y = 2 * text.LINE_H + 4
        for s, color in tail:
            text.draw_text(fb, x0, y, s, color)
            y += text.LINE_H
        self._draw_scrollbar(fb)
        self._draw_input()
        if self.mouse is not None:
            self.mouse.draw_cursor(fb)

    def _scroll_rect(self):
        x0 = (self.fb.width - appbar.right_sidebar_w()) - 12
        y0 = 2 * text.LINE_H + 4
        y1 = self.fb.height - 2 * text.LINE_H - 8
        return (x0, y0, 12, max(y1 - y0, 8))

    def _draw_scrollbar(self, fb):
        x0, y0, w, h = self._scroll_rect()
        fb.fill_rect(x0, y0, w, h, 30, 36, 48)
        total = len(self.lines)
        if total <= self.max_out:
            return
        span = max(int(h * self.max_out / total), 8)
        max_scroll = self._max_scroll()
        frac = self.scroll / max_scroll if max_scroll else 0.0
        ty = int(y0 + 2 + frac * (h - 4 - span))
        ty = min(ty, y0 + h - 2 - span)
        fb.fill_rect(x0 + 2, ty, w - 4, span, 120, 145, 185)

    def _scrollbar_hit(self, x, y):
        x0, y0, w, h = self._scroll_rect()
        if not (x0 <= x < x0 + w and y0 <= y < y0 + h):
            return False
        total = len(self.lines)
        if total <= self.max_out:
            return True
        max_scroll = self._max_scroll()
        span = max(int(h * self.max_out / total), 8)
        frac = self.scroll / max_scroll if max_scroll else 0.0
        ty = int(y0 + 2 + frac * (h - 4 - span))
        if y < ty:
            self.scroll = min(max_scroll, self.scroll + self.max_out)
        elif y >= ty + span:
            self.scroll = max(0, self.scroll - self.max_out)
        else:
            # thumb jump: the clicked point becomes the thumb top
            f = (y - y0 - 2 - span / 2) / max(h - 4, 1)
            self.scroll = int(max(0.0, min(1.0, f)) * max_scroll)
        self._draw()
        return True

    def _redraw_input(self):
        fb = self.fb
        if self.mouse is not None:
            # partial repaint: remove the cursor first so the re-capture
            # below cannot store the painted cursor (an afterimage)
            self.mouse.clear_cursor(fb)
        iy = fb.height - text.LINE_H - 6
        fb.fill_rect(0, iy, fb.width, fb.height - iy, *BG)
        self._draw_input()
        if self.mouse is not None:
            self.mouse.draw_cursor(fb)   # repaint the cursor over this row

    def _draw_input(self):
        fb = self.fb
        iy = fb.height - text.LINE_H - 6
        px = text.draw_text(fb, self._content_x(), iy, PROMPT, PROMPT_COLOR)
        before = self.line[:self.pos]
        after = self.line[self.pos:]
        cx = text.draw_text(fb, px, iy, before, TEXT_COLOR)
        fb.fill_rect(cx + 2, iy + 2, 2, text.LINE_H - 4, *CURSOR)
        text.draw_text(fb, cx + 6, iy, after, TEXT_COLOR)
