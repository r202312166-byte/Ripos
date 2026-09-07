# editor.py -- an ISE-style text editor for Ripos (pure Python).
#
# A full-screen tkinter app: a multi-line editor on top, an interactive
# Python console pane at the bottom (like PowerShell ISE) and a toolbar.
# F5 runs the current buffer in the console pane (stdout is captured
# into the pane), F6 toggles focus between the editor and the console,
# F2 saves the buffer to its path (or the path in the toolbar entry;
# /home is the writable RAM disk), Esc quits back to the shell (or the
# file manager).  Boot it from the shell with:  edit [path]
#
# Editing: click / arrow keys place the cursor; Shift+arrows/Home/End
# extend a selection; double-click selects a word, triple-click a line;
# dragging the mouse selects; right-click places the cursor, middle-click
# pastes; Ctrl+C/V/X copy/paste/cut the selection (whole buffer when
# there is none); Ctrl+Z undoes.  Long lines scroll horizontally and the
# editor has vertical + horizontal scrollbars (mouse wheel scrolls).

import kern
import os
import sys
import traceback
import tkinter as tk
import text as textmod
import settings
import i18n
from keyboard import Keyboard


def tr(key, **kw):
    """Translate a UI string (language from the settings app)."""
    i18n.sync_from_settings()
    return i18n.trf(key, **kw) if kw else i18n.tr(key)

# colors come from the active theme (settings.apply_theme() re-colors a
# running editor on switch)
_T = settings.theme()
BG = _T['bg']
FG = _T['fg']
PROMPT = _T['prompt']
ERROR = _T['err']
INFO = _T['status_fg']
OK = _T['ok']
CURSOR = _T['cursor']
SEL_BG = (60, 88, 128)
SB = 8   # scrollbar thickness


class _PaneWriter:
    """A file-like object that appends to a ShellPane (stdout capture
    while a script runs; the pane repaints once the run finishes)."""

    def __init__(self, pane):
        self.pane = pane

    def write(self, s):
        self.pane.write(str(s))

    def flush(self):
        pass


class EditorView(tk.Widget):
    """A multi-line text surface: a list of lines, a cursor, a selection
    (keyboard shift + mouse drag), horizontal + vertical scrolling and
    scrollbars.  Keyboard and mouse editing (click to place the cursor)."""

    focusable = True

    def __init__(self, master, fg=FG, bg=BG, cursor=CURSOR, **kw):
        super().__init__(master, **kw)
        self.fg = fg
        self.bg = bg
        self.cursor = cursor
        self.lines = [""]
        self.row = 0
        self.col = 0
        self.top = 0
        self.col_off = 0          # horizontal scroll offset (characters)
        self.sel_a = None         # selection anchor (row, col)
        self.sel_b = None         # selection end (row, col)
        self.sel_start = None     # normalized (start <= end)
        self.sel_end = None
        self._vsb = None          # vertical scrollbar rect (drawn position)
        self._hsb = None
        self._natw = 240
        self._nath = 240
        self.w, self.h = self._natw, self._nath
        self._opts.update(fg=fg, bg=bg)
        self._undo = []   # M9.6: snapshots of (lines, row, col) for Ctrl+Z
        self.bind("<Return>", lambda e: self._newline())
        self.bind("<Tab>", lambda e: self._insert("    "))

    # ---- editing ----------------------------------------------------

    def load(self, text):
        self.lines = text.split("\n") if text else [""]
        if not self.lines:
            self.lines = [""]
        self.row = 0
        self.col = 0
        self.top = 0
        self.col_off = 0
        self.sel_a = self.sel_b = None
        self._norm_sel()

    def get_text(self):
        return "\n".join(self.lines)

    def _snap(self):
        self._undo.append(([ln[:] for ln in self.lines], self.row, self.col))
        if len(self._undo) > 500:
            del self._undo[0]

    def undo(self):
        if not self._undo:
            return False
        self.lines, self.row, self.col = self._undo.pop()
        self._clamp()
        return True

    def insert(self, s):
        self._snap()
        self._delete_selection()
        self._insert(s)
        self._clamp()
        return True

    # ---- geometry helpers -------------------------------------------

    def _visible(self):
        return max(1, (self.h - SB - 2 * self.pady) // textmod.LINE_H)

    def _cols_visible(self):
        if self.w <= 0:
            return 40
        return max(1, (self.w - 2 * self.padx - SB) // textmod.char_width('0'))

    def _clamp(self):
        self.row = max(0, min(len(self.lines) - 1, self.row))
        ln = self.lines[self.row]
        self.col = max(0, min(len(ln), self.col))
        vis = self._visible()
        if self.row < self.top:
            self.top = self.row
        if self.row >= self.top + vis:
            self.top = max(0, self.row - vis + 1)
        # keep the cursor horizontally in view
        if self.w > 0:
            cols = self._cols_visible()
            if self.col < self.col_off:
                self.col_off = max(0, self.col)
            if self.col >= self.col_off + cols:
                self.col_off = max(0, self.col - cols + 1)

    # ---- selection ---------------------------------------------------

    def _norm_sel(self):
        if self.sel_a is None or self.sel_b is None:
            self.sel_start = self.sel_end = None
            return
        if self.sel_a <= self.sel_b:
            self.sel_start, self.sel_end = self.sel_a, self.sel_b
        else:
            self.sel_start, self.sel_end = self.sel_b, self.sel_a

    def _selection_active(self):
        return self.sel_start is not None

    def _selected_text(self):
        if self.sel_start is None:
            return ""
        r0, c0 = self.sel_start
        r1, c1 = self.sel_end
        if r0 == r1:
            return self.lines[r0][c0:c1]
        parts = [self.lines[r0][c0:]]
        for i in range(r0 + 1, r1):
            parts.append(self.lines[i])
        parts.append(self.lines[r1][:c1])
        return "\n".join(parts)

    def _sel_range(self, i):
        """Visible char range of the selection on line i, or None."""
        if self.sel_start is None:
            return None
        r0, c0 = self.sel_start
        r1, c1 = self.sel_end
        if r0 == r1:
            if i != r0:
                return None
            return (c0, c1)
        if i == r0:
            return (c0, len(self.lines[i]))
        if i == r1:
            return (0, c1)
        if r0 < i < r1:
            return (0, len(self.lines[i]))
        return None

    def _select_word(self, r, c):
        ln = self.lines[r]

        def isw(ch):
            return ch.isalnum() or ch == '_'

        s = c
        while s > 0 and isw(ln[s - 1]):
            s -= 1
        e = c
        while e < len(ln) and isw(ln[e]):
            e += 1
        if s == e:   # no word at the click point: select the character
            e = min(c + 1, len(ln))
        self.sel_a = (r, s)
        self.sel_b = (r, e)
        self._norm_sel()

    def _select_line(self, r):
        self.sel_a = (r, 0)
        self.sel_b = (r, len(self.lines[r]))
        self._norm_sel()

    def _delete_selection(self):
        """Delete the selection, if any; cursor moves to its start."""
        if self.sel_start is None:
            return False
        r0, c0 = self.sel_start
        r1, c1 = self.sel_end
        if r0 == r1:
            ln = self.lines[r0]
            self.lines[r0] = ln[:c0] + ln[c1:]
            self.row, self.col = r0, c0
        else:
            merged = self.lines[r0][:c0] + self.lines[r1][c1:]
            self.lines[r0] = merged
            del self.lines[r0 + 1:r1 + 1]
            self.row, self.col = r0, c0
        self.sel_a = self.sel_b = None
        self._norm_sel()
        self._clamp()
        return True

    # ---- editing ------------------------------------------------------

    def _insert(self, s):
        ln = self.lines[self.row]
        self.lines[self.row] = ln[:self.col] + s + ln[self.col:]
        self.col += len(s)

    def _newline(self):
        ln = self.lines[self.row]
        self.lines[self.row] = ln[:self.col]
        self.lines.insert(self.row + 1, ln[self.col:])
        self.row += 1
        self.col = 0
        return True

    def _delete(self):
        ln = self.lines[self.row]
        if self.col < len(ln):
            self.lines[self.row] = ln[:self.col] + ln[self.col + 1:]
            return True
        if self.row < len(self.lines) - 1:
            self.lines[self.row] = ln + self.lines[self.row + 1]
            del self.lines[self.row + 1]
            return True
        return False

    def on_char(self, ch):
        if ch == "\n":
            self._snap()
            self._delete_selection()
            return self._newline()
        if ch and 32 <= ord(ch) < 127:
            self._snap()
            self._delete_selection()
            self._insert(ch)
            return True
        return False

    def on_backspace(self):
        if self.sel_start is not None:
            self._snap()
            self._delete_selection()
            return True
        if self.col > 0:
            self._snap()
            ln = self.lines[self.row]
            self.lines[self.row] = ln[:self.col - 1] + ln[self.col:]
            self.col -= 1
            return True
        if self.row > 0:
            self._snap()
            prev = self.lines[self.row - 1]
            self.col = len(prev)
            self.lines[self.row - 1] = prev + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1
            return True
        return False

    def _shift_held(self):
        try:
            kb = self._root().kb
            return kb is not None and kb.shift_down()
        except Exception:
            return False

    def _mv(self, nr, nc, shift):
        if shift and self.sel_a is None:
            self.sel_a = (self.row, self.col)
        self.row, self.col = nr, nc
        self._clamp()
        if shift:
            self.sel_b = (self.row, self.col)
        else:
            self.sel_a = self.sel_b = None
        self._norm_sel()
        return True

    def on_key_name(self, name):
        shift = self._shift_held()
        if name == "left":
            if self.col > 0:
                return self._mv(self.row, self.col - 1, shift)
            if self.row > 0:
                return self._mv(self.row - 1,
                                len(self.lines[self.row - 1]), shift)
        elif name == "right":
            ln = self.lines[self.row]
            if self.col < len(ln):
                return self._mv(self.row, self.col + 1, shift)
            if self.row < len(self.lines) - 1:
                return self._mv(self.row + 1, 0, shift)
        elif name == "up":
            if self.row > 0:
                return self._mv(self.row - 1,
                                min(self.col, len(self.lines[self.row - 1])),
                                shift)
        elif name == "down":
            if self.row < len(self.lines) - 1:
                return self._mv(self.row + 1,
                                min(self.col, len(self.lines[self.row + 1])),
                                shift)
        elif name == "home":
            return self._mv(self.row, 0, shift)
        elif name == "end":
            return self._mv(self.row, len(self.lines[self.row]), shift)
        elif name == "pgup":
            return self._mv(max(0, self.row - self._visible()),
                            self.col, shift)
        elif name == "pgdn":
            return self._mv(min(len(self.lines) - 1,
                                self.row + self._visible()),
                            self.col, shift)
        elif name == "del":
            if self.sel_start is not None:
                self._snap()
                self._delete_selection()
                return True
            return self._delete()
        else:
            return False
        return False

    # ---- mouse --------------------------------------------------------

    def _pos_at(self, x, y):
        vis = self._visible()
        r = (y - self.y - self.pady) // textmod.LINE_H + self.top
        r = max(0, min(len(self.lines) - 1, r))
        ln = self.lines[r]
        xx = x - self.x - self.padx
        acc = 0
        vis_col = 0
        for ch in ln[self.col_off:]:
            w = textmod.char_width(ch)
            if acc + w // 2 <= xx:
                acc += w
                vis_col += 1
            else:
                break
        col = self.col_off + vis_col
        return r, max(0, min(len(ln), col))

    def on_click(self, x, y, button, dbl, clicks=1):
        if button == 1:
            if self._scrollbar_click(x, y):
                return
            r, c = self._pos_at(x, y)
            self.row, self.col = r, c
            self._clamp()
            self.sel_a = self.sel_b = None
            if clicks >= 3:
                self._select_line(r)
            elif clicks == 2:
                self._select_word(r, c)
            return
        if button == 4:   # middle click: paste the clipboard
            try:
                import clipboard
                t = clipboard.paste()
            except Exception:
                t = ""
            if t:
                self.insert(t)
            return
        if button == 2:   # right click: place the cursor
            r, c = self._pos_at(x, y)
            self.row, self.col = r, c
            self._clamp()
            self.sel_a = self.sel_b = None

    def on_drag(self, x, y):
        """Mouse moved with the left button held: extend the selection."""
        if self.sel_a is None:
            self.sel_a = (self.row, self.col)
        r, c = self._pos_at(x, y)
        self.row, self.col = r, c
        self._clamp()
        self.sel_b = (self.row, self.col)
        self._norm_sel()

    def on_release(self, x, y):
        pass

    def on_wheel(self, delta):
        vis = self._visible()
        if delta > 0:
            self.top = max(0, self.top - 3)
        else:
            self.top = min(max(0, len(self.lines) - vis), self.top + 3)
        return True

    def _scrollbar_click(self, x, y):
        total = len(self.lines)
        vis = self._visible()
        if self._vsb is not None:
            x0, y0, w, h = self._vsb
            if x0 <= x < x0 + w:
                if total > vis:
                    frac = (y - y0 - 2) / max(h - 4, 1)
                    self.top = int(frac * max(0, total - vis))
                    self.top = max(0, min(max(0, total - vis), self.top))
                    self._clamp()
                return True
        maxlen = max((len(ln) for ln in self.lines), default=0)
        if self._hsb is not None and maxlen > self._cols_visible():
            x0, y0, w, h = self._hsb
            if y0 <= y < y0 + h:
                cols = self._cols_visible()
                frac = (x - x0 - 2) / max(w - 4, 1)
                self.col_off = int(frac * max(0, maxlen - cols))
                self.col_off = max(0,
                                   min(max(0, maxlen - cols), self.col_off))
                self._clamp()
                return True
        return False

    # ---- rendering -----------------------------------------------------

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        vis = self._visible()
        for k in range(vis):
            i = self.top + k
            if i >= len(self.lines):
                break
            y = oy + self.pady + k * textmod.LINE_H
            self._draw_line(fb, ox, oy, i, y)
        self._draw_scrollbars(fb, ox, oy)

    def _draw_line(self, fb, ox, oy, i, y):
        ln = self.lines[i]
        cols = self._cols_visible()
        start = self.col_off
        vis = ln[start:start + cols] if start < len(ln) else ""
        sel = self._sel_range(i)
        px = ox + self.padx
        if sel is None:
            if vis:
                textmod.draw_text(fb, px, y, vis, self.fg)
        else:
            a = max(0, sel[0] - start)
            b = min(len(vis), sel[1] - start)
            pre = vis[:a]
            if pre:
                textmod.draw_text(fb, px, y, pre, self.fg)
            cx0 = px + textmod.text_width(pre)
            seg = vis[a:b]
            sw = textmod.text_width(seg)
            fb.fill_rect(cx0, y, max(sw, 1), textmod.LINE_H, *SEL_BG)
            if seg:
                textmod.draw_text(fb, cx0, y, seg, self.fg)
            after = vis[b:]
            if after:
                textmod.draw_text(fb, cx0 + sw, y, after, self.fg)
        if i == self.row:
            cx = px + textmod.text_width(ln[self.col_off:self.col])
            fb.fill_rect(cx, y + 2, 2, textmod.LINE_H - 4, *self.cursor)

    def _draw_scrollbars(self, fb, ox, oy):
        total = len(self.lines)
        vis = self._visible()
        # vertical scrollbar (right edge)
        x0 = ox + self.w - SB
        fb.fill_rect(x0, oy, SB, self.h, 26, 32, 42)
        if total > vis:
            track = max(self.h - 4, 1)
            span = max(int(track * vis / total), 6)
            frac = self.top / (total - vis) if total > vis else 0.0
            ty = int(oy + 2 + frac * (track - span))
            fb.fill_rect(x0 + 2, ty, SB - 4, span, 120, 145, 185)
            self._vsb = (x0, oy, SB, self.h)
        else:
            self._vsb = None
        # horizontal scrollbar (bottom edge), when any line is too wide
        cols = self._cols_visible()
        maxlen = max((len(ln) for ln in self.lines), default=0)
        y0 = oy + self.h - SB
        fb.fill_rect(ox, y0, self.w, SB, 26, 32, 42)
        if maxlen > cols:
            track = max(self.w - 4, 1)
            span = max(int(track * cols / maxlen), 6)
            frac = self.col_off / (maxlen - cols) if maxlen > cols else 0.0
            tx = int(ox + 2 + frac * (track - span))
            fb.fill_rect(tx, y0 + 2, span, SB - 4, 120, 145, 185)
            self._hsb = (ox, y0, self.w, SB)
        else:
            self._hsb = None


class ShellPane(tk.Widget):
    """The bottom console: an interactive Python pane with scrollback,
    prompt, input line, history and a persistent namespace."""

    focusable = True

    def __init__(self, master, height=9, fg=FG, bg=(14, 18, 24), **kw):
        super().__init__(master, **kw)
        self.height = height
        self.fg = fg
        self.bg = bg
        self.lines = []
        self.line = ""
        self.history = []
        self.hist = -1
        self.ns = {"__name__": "__console__"}
        self._natw = 240
        self._nath = height * textmod.LINE_H + 2 * self.pady
        self.w, self.h = self._natw, self._nath
        self._opts.update(fg=fg, bg=bg)
        self.bind("<Return>", lambda e: self._submit())

    def write(self, s, color=None):
        if color is None:
            color = self.fg
        for part in str(s).split("\n"):
            self.lines.append((part, color))
        if len(self.lines) > 600:
            self.lines = self.lines[-300:]

    def _out(self, s, color=INFO):
        self.write(s, color)
        self._root().redraw()

    # ---- keyboard ----------------------------------------------------

    def on_char(self, ch):
        if ch == "\n":
            self._submit()
            return True
        if ch and 32 <= ord(ch) < 127:
            self.line += ch
            return True
        return False

    def on_backspace(self):
        if self.line:
            self.line = self.line[:-1]
            return True
        return False

    def on_key_name(self, name):
        n = len(self.history)
        if n == 0:
            return False
        if name == "up":
            self.hist = n - 1 if self.hist == -1 else max(0, self.hist - 1)
        elif name == "down":
            if self.hist == -1:
                return False
            self.hist -= 1
            if self.hist < -1:
                self.hist = -1
        else:
            return False
        self.line = "" if self.hist == -1 else self.history[self.hist]
        return True

    def _submit(self):
        line = self.line
        self.line = ""
        self.hist = -1
        self._out(tr("ed.prompt") + line, PROMPT)
        if line.strip():
            self.history.append(line)
            self._eval(line)
        self._root().redraw()

    def _eval(self, line):
        try:
            code = compile(line, "<console>", "single")
        except SyntaxError:
            self._out(traceback.format_exc().strip(), ERROR)
            return
        old = sys.stdout
        sys.stdout = _PaneWriter(self)
        try:
            exec(code, self.ns)
        except SystemExit:
            pass
        except Exception:
            self._out(traceback.format_exc().strip(), ERROR)
        finally:
            sys.stdout = old

    def run_script(self, src, name="<buffer>"):
        """Execute src in a FRESH namespace, capturing stdout into the
        pane (this is the editor's "run the file", F5)."""
        n0 = len(self.lines)
        self._out("=== run %s ===" % name, PROMPT)
        try:
            code = compile(src, name, "exec")
        except SyntaxError:
            self._out(traceback.format_exc().strip(), ERROR)
            return
        old = sys.stdout
        sys.stdout = _PaneWriter(self)
        try:
            ns = {"__name__": "__main__"}
            exec(code, ns)
        except SystemExit:
            pass
        except Exception:
            self._out(traceback.format_exc().strip(), ERROR)
        finally:
            sys.stdout = old
        self._out("=== end %s ===" % name, INFO)
        # echo the produced output to the serial console so gates can read it
        kern.write("editor: run %s ->\n" % name)
        for s, c in self.lines[n0:]:
            kern.write(s + "\n")
        kern.write("editor: run done\n")
        self._root().redraw()

    def draw(self, fb, ox, oy):
        super().draw(fb, ox, oy)
        fb.fill_rect(ox, oy, self.w, self.h, *self.bg)
        vis = max(1, self.h // textmod.LINE_H)
        tail = self.lines[-(vis - 1):] if vis > 1 else []
        y = oy + self.pady
        for s, color in tail:
            textmod.draw_text(fb, ox + self.padx, y, s, color)
            y += textmod.LINE_H
        px = textmod.draw_text(fb, ox + self.padx, y, tr("ed.prompt"), PROMPT)
        textmod.draw_text(fb, px, y, self.line, self.fg)


class Editor:
    def __init__(self, root, on_quit, path=None):
        self.root = root
        self.on_quit = on_quit
        self.path = path
        self._build()
        if path:
            self.load(path)

    def _build(self):
        r = self.root
        # children order = focus order: the editor must be the first
        # focusable widget.  Pack order is separate (pack sequence):
        # status at the very bottom, console pane above it, then the
        # editor expands into the rest.
        self.editor = EditorView(r)
        self.status = tk.Label(r, text="", fg=INFO, bg=(22, 26, 34),
                               padx=6, pady=2)
        self.status.pack(side="bottom", fill="x")
        self.pane = ShellPane(r, height=9)
        self.pane.pack(side="bottom", fill="x")
        self.editor.pack(fill="both", expand=True)
        bar = tk.Frame(r, bg=(30, 36, 48))
        self.btn_open = tk.Button(bar, text=tr("ed.open"), command=self._open_path_entry)
        self.btn_run = tk.Button(bar, text=tr("ed.run_f5"), command=self.run_file)
        self.btn_save = tk.Button(bar, text=tr("ed.save_f2"), command=self.save_file)
        self.btn_new = tk.Button(bar, text=tr("ed.new"), command=self.new_file)
        self.btn_quit = tk.Button(bar, text=tr("ed.quit"), command=self.quit_app)
        for b in (self.btn_open, self.btn_run, self.btn_save, self.btn_new, self.btn_quit):
            b.pack(side="left", padx=2, pady=2)
        self.path_entry = tk.Entry(bar, width=26)
        self.path_entry.pack(side="left", padx=4)
        self.path_entry.bind("<Return>", lambda e: self._open_path_entry())
        bar.pack(side="top", fill="x")
        r.bind("<F5>", lambda e: self.run_file())
        r.bind("<F6>", lambda e: self._toggle_focus())
        r.bind("<F2>", lambda e: self.save_file())
        # M9.6 clipboard + undo hotkeys (tk's Ctrl+letter pre-pass)
        r.bind("<Control-z>", lambda e: self.undo())
        r.bind("<Control-c>", lambda e: self.copy_all())
        r.bind("<Control-v>", lambda e: self.paste())
        r.bind("<Control-x>", lambda e: self.cut_all())
        r.bind("<Control-a>", lambda e: self.select_all())
        # NOTE: '<Escape>' is bound by editor.run() (it also restores the
        # caller keyboard); the Quit button calls quit_app directly.

    def load(self, path):
        try:
            low = path.lower()
            if low.endswith(".rtf"):
                import rtf
                with open(path, "rb") as f:
                    text = rtf.load_rtf(f.read())
            elif low.endswith(".docx"):
                import docx
                with open(path, "rb") as f:
                    text = docx.load_docx(f.read())
            else:
                with open(path, "r") as f:
                    text = f.read()
        except OSError as e:
            self.pane._out(tr("ed.open_fail", p=path, e=e), ERROR)
            return
        except Exception as e:
            self.pane._out(tr("ed.open_fail", p=path, e=e), ERROR)
            return
        self.editor.load(text)
        self.path = path
        self.path_entry.set(path)
        self.status.text = tr("ed.opened", p=path, n=len(self.editor.lines))
        self.pane._out(tr("ed.opened", p=path, n=len(self.editor.lines)), OK)
        self.root.redraw()

    def new_file(self):
        self.editor.load("")
        self.path = None
        self.path_entry.set("")
        self.status.text = tr("ed.new_buffer")
        self.root.redraw()

    def undo(self):
        if self.editor.undo():
            self.pane._out(tr("ed.undo"), OK)
            self.root.redraw()

    def copy_all(self):
        import clipboard
        sel = self.editor._selected_text()
        text = sel if sel else self.editor.get_text()
        clipboard.copy(text)
        self.pane._out(tr("ed.copied", n=len(text)), OK)

    def paste(self):
        import clipboard
        text = clipboard.paste()
        if text:
            self.editor.insert(text)
            self.root.redraw()

    def cut_all(self):
        import clipboard
        sel = self.editor._selected_text()
        if sel:
            clipboard.copy(sel)
            self.editor._snap()
            self.editor._delete_selection()
        else:
            clipboard.copy(self.editor.get_text())
            self.editor.load("")
        self.root.redraw()

    def select_all(self):
        self.editor.sel_a = (0, 0)
        self.editor.sel_b = (len(self.editor.lines) - 1,
                             len(self.editor.lines[-1]))
        self.editor._norm_sel()
        self.editor.row = 0
        self.editor.col = 0
        self.root.redraw()

    def save_file(self):
        """Save the buffer.  Uses self.path, or the path in the toolbar
        entry; /home files go to the writable RAM disk."""
        if not self.path:
            p = self.path_entry.get().strip()
            if not p:
                self.pane._out(tr("ed.no_path"), ERROR)
                return
            self.path = p
        try:
            low = self.path.lower()
            if low.endswith(".rtf"):
                import rtf
                with open(self.path, "wb") as f:
                    f.write(rtf.save_rtf(self.editor.get_text()))
            elif low.endswith(".docx"):
                import docx
                with open(self.path, "wb") as f:
                    f.write(docx.save_docx(self.editor.get_text()))
            else:
                with open(self.path, "w") as f:
                    f.write(self.editor.get_text())
        except OSError as e:
            self.pane._out(tr("ed.save_fail", p=self.path, e=e), ERROR)
            return
        except Exception as e:
            self.pane._out(tr("ed.save_fail", p=self.path, e=e), ERROR)
            return
        self.path_entry.set(self.path)
        self.status.text = tr("ed.saved", p=self.path, n=len(self.editor.lines))
        self.pane._out(tr("ed.saved", p=self.path, n=len(self.editor.get_text())), OK)
        kern.write("editor: saved %s\n" % self.path)
        self.root.redraw()

    def _open_path_entry(self):
        p = self.path_entry.get().strip()
        if p:
            self.load(p)

    def run_file(self):
        kern.write("editor: run buffer (F5)\n")
        self.pane.run_script(self.editor.get_text(), self.path or "<buffer>")

    def _toggle_focus(self):
        fs = self.root._focusables()
        if not fs:
            return
        cur = self.root._focused()
        target = self.pane if cur is self.editor else self.editor
        if target in fs:
            self.root._focus_idx = fs.index(target)
            self.root.redraw()

    def quit_app(self):
        kern.write("editor: quit\n")
        self.root.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    """Start the editor.  kb is the CALLER keyboard (restored on quit);
    returns immediately (the kernel event loop drives the UI)."""
    kb2 = Keyboard()
    root = tk.Tk(title="Ripos Editor", sidebar=True)
    # start BEFORE building the tree: pack() redraws, and the lazy _ensure
    # must not create a second Keyboard (it would steal kern.on_key from kb2)
    root.start(fb, kb2, mouse)
    # app window system: the title-bar '-'/'X' and the left sidebar
    root._prev_kb = kb
    root._back = on_quit
    root.begin_build()          # one repaint at the end, not 20+
    app = Editor(root, on_quit, path)
    root.end_build()

    def quit_app():
        # restore the caller keyboard FIRST so the shell is live even if
        # the redraw (on_quit) is slow; then destroy + repaint
        kb.activate()
        app.quit_app()

    root._close_handler = quit_app
    app.btn_quit.command = quit_app
    root.bind("<Escape>", lambda e: quit_app())
    if path is None:
        app.pane._out(tr("ed.hint"), INFO)
    root.mainloop()
    kern.write("editor: run returned (event-driven)\n")
