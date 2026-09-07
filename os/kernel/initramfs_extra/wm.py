"""wm.py -- window manager for the Ripos GUI (pure Python).

Implements z-ordered overlapping windows, keyboard focus, window movement
with the arrow keys, a terminal window, and live language switching
(Ctrl+L toggles English <-> Chinese).  The kernel only supplies the
framebuffer (kern.fb_mem) and keyboard events (kern.on_key); everything
here is Python.
"""

import kern
import i18n
import text
import settings

# palette (from the active theme; settings.apply_theme() re-colors the
# desktop on switch)
_T = settings.theme()
BG = _T['bg']
TITLE_BG = _T['title_bg']
TITLE_BG_FOCUS = _T['accent']
WIN_BG = _T['panel']
BORDER = _T['sidebar_edge']
TEXT = _T['fg']
TITLE_TEXT = _T['title_fg']
HINT_TEXT = _T['status_fg']

TITLE_H = 16


class Window:
    """A movable, z-ordered window with a title bar and content lines."""

    def __init__(self, title_key, x, y, w, h):
        self.title_key = title_key
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def content(self, desk):
        return []

    def on_char(self, ch):
        return False  # False = no redraw needed

    def move(self, dx, dy, sw, sh):
        self.x = max(0, min(sw - self.w, self.x + dx))
        self.y = max(0, min(sh - self.h, self.y + dy))


class SysInfo(Window):
    def content(self, desk):
        t = kern.tick()
        used, total = kern.alloc_stats()
        return [
            i18n.tr('sys.kernel'),
            i18n.tr('sys.python', py='3.12.10'),
            i18n.tr('sys.modules', n=52),
            i18n.tr('sys.uptime', s=t // 1000),
            i18n.tr('sys.heap', used=used, total=total),
            i18n.tr('sys.fb', w=desk.fb.width, h=desk.fb.height, fmt=desk.fb.format),
        ]


class Terminal(Window):
    def __init__(self, title_key, x, y, w, h):
        super().__init__(title_key, x, y, w, h)
        self.line = ""
        self.runs = 0

    def content(self, desk):
        return [
            i18n.tr('term.prompt') + ' ' + self.line,
            '',
            i18n.tr('term.status'),
        ]

    def on_char(self, ch):
        if ch == '\n':
            self.runs += 1
            kern.write('m7: term line %d: %s\n' % (self.runs, self.line))
            self.line = ""
            return True
        if ch == '\b':
            self.line = self.line[:-1]
            return True
        if ch and ord(ch) >= 32:
            self.line += ch
            return True
        return False


class LangInfo(Window):
    def content(self, desk):
        return [
            i18n.tr('lang.label') + ' ' + i18n.name(),
            'en: ' + i18n.tr('lang.en') + '    zh: ' + i18n.tr('lang.zh'),
            i18n.tr('lang.hint'),
        ]


class Desktop:
    """The root: owns the framebuffer, the keyboard, window list and focus."""

    def __init__(self, fb, kb, windows, focus=0, lang='en'):
        self.fb = fb
        self.kb = kb
        self.windows = windows
        self.focus = focus % len(windows)
        i18n.set_language(lang)
        kb.on_event(self.on_key)
        self.redraw()
        kern.write('m7: desktop %dx%d focus=%d windows=%d\n'
                   % (fb.width, fb.height, self.focus, len(windows)))
        self._tick()

    # ---- keyboard -------------------------------------------------

    def on_key(self, ev):
        # kern events carry (name: str, ch: int ascii code or 0, pressed: int)
        name, ch, pressed = ev
        if not pressed:
            return
        if name == 'ctrl':
            return  # modifier state tracked by the keyboard driver
        if ch == ord('l') and self.kb.is_down('ctrl'):
            lang = 'zh' if i18n.current == 'en' else 'en'
            i18n.set_language(lang)
            kern.write('m7: lang=%s\n' % lang)
            self.redraw()
            return
        if name == 'tab':
            self.focus = (self.focus + 1) % len(self.windows)
            kern.write('m7: focus=%d (%s)\n'
                       % (self.focus, self.windows[self.focus].title_key))
            self.redraw()
            return
        if name in ('left', 'right', 'up', 'down'):
            dx = -8 if name == 'left' else (8 if name == 'right' else 0)
            dy = -8 if name == 'up' else (8 if name == 'down' else 0)
            w = self.windows[self.focus]
            w.move(dx, dy, self.fb.width, self.fb.height)
            kern.write('m7: move win %d -> (%d,%d)\n' % (self.focus, w.x, w.y))
            self.redraw()
            return
        if ch is None or ch == 0:
            return
        if name == 'backspace':
            c = '\b'
        elif name == 'enter':
            c = '\n'
        elif 32 <= ch < 127:
            c = chr(ch)
        else:
            return
        w = self.windows[self.focus]
        if hasattr(w, 'on_char') and w.on_char(c):
            self.redraw()

    # ---- rendering -------------------------------------------------

    def redraw(self):
        fb = self.fb
        fb.clear(*BG)
        text.draw_text(fb, 8, 4, i18n.tr('os.title'), TITLE_TEXT)
        text.draw_text(fb, 8, 24, i18n.tr('os.subtitle'), HINT_TEXT)
        hint = i18n.tr('desktop.hint')
        text.draw_text(fb, 8, fb.height - text.LINE_H - 2, hint, HINT_TEXT)
        # z-order: non-focused first, focused window on top
        for i, w in enumerate(self.windows):
            if i != self.focus:
                self._draw_window(w, False)
        self._draw_window(self.windows[self.focus], True)

    def _draw_window(self, w, focused):
        fb = self.fb
        fb.fill_rect(w.x, w.y, w.w, w.h, *WIN_BG)
        tb = TITLE_BG_FOCUS if focused else TITLE_BG
        fb.fill_rect(w.x, w.y, w.w, TITLE_H, *tb)
        text.draw_text(fb, w.x + 4, w.y + 2, i18n.tr(w.title_key), TITLE_TEXT)
        cy = w.y + TITLE_H + 2
        if hasattr(w, 'draw_content'):
            # widget trees (tk.py) draw themselves inside the window body
            w.draw_content(fb, w.x, cy, w.w, w.h - TITLE_H - 2)
        else:
            for line in w.content(self):
                if line:
                    text.draw_text(fb, w.x + 6, cy, line, TEXT)
                cy += text.LINE_H
        fb.draw_rect(w.x, w.y, w.w, w.h, *BORDER)

    def _tick(self):
        def again():
            self.redraw()
            kern.after(3000, again)
        kern.after(3000, again)
