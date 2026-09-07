# fm.py -- a Windows-Explorer-like file manager for Ripos (pure Python).
#
# A full-screen tkinter app: a toolbar (Up / Back / Forward / Home /
# New / Del / Run / Open / Editor / Quit), a path bar, a file list
# (folders first, sizes right-aligned, Explorer-style) and a status bar.
# It accepts a mouse (click selects, double-click opens) and a keyboard
# (arrows, Enter, Del = delete, Backspace = parent folder, Esc = quit).
# It does NOT need the wm desktop -- the window is the whole screen --
# and quitting returns to the M8 shell.  Boot it from the shell with:  fm
#
# Run executes the selected .py file (output echoed to the status bar and
# the serial console).  Open shows a panel on the right listing the apps
# that can open the selected file (configurable in /home/settings.json:
# {"open_with": {".py": ["editor", "run"], ...}}); picking one opens it.
#
# /home is the writable RAM disk: New creates a folder there, Del deletes
# the selected entry (files anywhere in /home, empty folders only).
# Double-clicking a text source opens it in the editor and an image
# opens it in the image viewer.

import kern
import os
import sys
import traceback
import tkinter as tk
import appbar
import settings
import i18n
from keyboard import Keyboard


def tr(key, **kw):
    """Translate a UI string (language from the settings app)."""
    i18n.sync_from_settings()
    return i18n.trf(key, **kw) if kw else i18n.tr(key)


_EDITABLE = (".py", ".txt", ".md", ".c", ".rs", ".json", ".toml",
             ".ini", ".cfg", ".sh", ".h", ".js", ".html", ".css",
             ".rtf", ".docx")   # the editor is format-aware (rtf.py/docx.py)
_IMAGE = (".png", ".jpg", ".jpeg")
_MEDIA = (".mp3", ".mp4", ".wav", ".aac")
_ARCHIVE = (".zip", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".xz")

# colors come from the active theme (settings.apply_theme() re-colors a
# running fm on switch; the toolbar panels read settings.c() per build)
TITLE_BG = settings.c('title_bg')
PATH_BG = settings.c('status')
STATUS_FG = settings.c('status_fg')


class _OutSink:
    """File-like object collecting print() output while a script runs."""

    def __init__(self, sink):
        self.sink = sink

    def write(self, s):
        self.sink.append(str(s))

    def flush(self):
        pass


def _fmt_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / (1024.0 * 1024.0))
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


class _FMList(tk.Listbox):
    """The file list; the Del key deletes the selected entry (the tk key
    routing sends named keys to the focused widget's on_key_name)."""

    def on_key_name(self, name):
        if name == 'del':
            if self._fm is not None:
                self._fm.delete_selected()
            return True
        return super().on_key_name(name)


class FileManager:
    def __init__(self, root, on_quit, start=None):
        self.root = root
        self.on_quit = on_quit
        self.path = start if start else "/"
        self.history = []
        self.forward = []
        self._panel_shown = False
        self._build()
        self.refresh()

    def _writable(self):
        """Only the /home RAM disk accepts mkdir/delete."""
        return self.path == "/home" or self.path.startswith("/home/")

    # ---- helpers --------------------------------------------------

    def _join(self, name):
        if self.path == "/":
            return "/" + name
        return self.path.rstrip("/") + "/" + name

    def _parent(self, p):
        p = p.rstrip("/")
        if p == "" or p == "/":
            return "/"
        return p.rsplit("/", 1)[0] or "/"

    # ---- UI --------------------------------------------------------

    def _build(self):
        r = self.root
        bar = tk.Frame(r, bg=settings.c('bar'))
        self.btn_up = tk.Button(bar, text=tr("fm.up"), command=self.go_up)
        self.btn_back = tk.Button(bar, text=tr("fm.back"), command=self.go_back)
        self.btn_fwd = tk.Button(bar, text=tr("fm.fwd"), command=self.go_forward)
        self.btn_home = tk.Button(bar, text=tr("fm.home"), command=self.go_home)
        self.btn_new = tk.Button(bar, text=tr("fm.new"), command=self.new_folder)
        self.btn_del = tk.Button(bar, text=tr("fm.del"), command=self.delete_selected)
        self.btn_run = tk.Button(bar, text=tr("fm.run"), command=self.run_selected)
        self.btn_open = tk.Button(bar, text=tr("fm.open"), command=self._toggle_open_panel)
        self.btn_edit = tk.Button(bar, text=tr("fm.editor"), command=self.open_editor)
        self.btn_quit = tk.Button(bar, text=tr("fm.quit"), command=self.quit_app)
        for b in (self.btn_up, self.btn_back, self.btn_fwd, self.btn_home,
                  self.btn_new, self.btn_del, self.btn_run, self.btn_open,
                  self.btn_edit, self.btn_quit):
            b.pack(side="left", padx=2, pady=2)
        bar.pack(side="top", fill="x")

        self.path_label = tk.Label(r, text="/", fg=settings.c('fg'),
                                   bg=PATH_BG, padx=6, pady=2)
        self.path_label.pack(side="top", fill="x")

        # "Open with" panel on the right (hidden until the Open button is
        # clicked; packed BEFORE the listbox so it claims the right strip)
        self.open_frame = tk.Frame(r, width=176,
                                   bg=settings.c('sidebar_bg'))
        tk.Label(self.open_frame, text=tr("fm.open_with"),
                 fg=settings.c('sidebar_fg'),
                 bg=settings.c('sidebar_bg'), padx=4, pady=3).pack(
                     side="top", fill="x")
        self.open_list = tk.Listbox(self.open_frame, height=8,
                                    on_activate=self._open_with_pick)
        self.open_list.pack(fill="both", expand=True, padx=4, pady=4)
        self.open_frame.pack_forget()

        self.listbox = _FMList(r, height=24, on_activate=self.activate,
                               on_select=self._update_open_panel)
        self.listbox._fm = self
        self.listbox.pack(fill="both", expand=True, padx=4, pady=4)
        self.listbox.focus_set()   # the file list owns the initial focus

        self.status = tk.Label(r, text="", fg=STATUS_FG,
                               bg=settings.c('status'), padx=6, pady=2)
        self.status.pack(side="bottom", fill="x")

        # NOTE: '<Escape>' is bound by fm.run() (it also restores the
        # caller keyboard); this class only handles Backspace itself.
        r.bind("<BackSpace>", lambda e: self.go_up())
        r.bind("<F7>", lambda e: self.new_folder())

    # ---- open-with panel -------------------------------------------

    def _toggle_open_panel(self):
        if self._panel_shown:
            self.open_frame.pack_forget()
            self._panel_shown = False
        else:
            self._panel_shown = True
            self._update_open_panel()
            # pack the panel BEFORE re-packing the listbox so the pack
            # sequence gives the panel the right strip
            self.open_frame.pack(side="right", fill="y")
            self.listbox.pack(fill="both", expand=True, padx=4, pady=4)
        self.root.redraw()

    def _update_open_panel(self):
        if not self._panel_shown:
            return
        name = self._selected_name()
        apps = appbar.open_with_for(name) if name else []
        if not apps:
            apps = ["editor"]
        self.open_list.set_items([(a, "") for a in apps])

    def _open_with_pick(self, index, item):
        app_name = item[0] if isinstance(item, (tuple, list)) else item
        sel = self._selected_name()
        if sel is None:
            return
        full = self._join(sel)
        kern.write("fm: open %s with %s\n" % (full, app_name))
        try:
            if app_name == "run":
                self.run_selected()
            elif app_name == "editor":
                self.open_editor(full)
            elif app_name == "imgview":
                self.open_image(full)
            elif app_name == "mplayer":
                self.open_media(full)
            elif app_name == "imgedit":
                self.open_imgedit(full)
            elif app_name == "arc":
                self.open_archive(full)
            else:
                self.status.text = tr("fm.unknown_app", a=app_name)
                self.root.redraw()
        except Exception as e:
            self.status.text = "open with %s: %r" % (app_name, e)
            self.root.redraw()

    def navigate(self, path):
        """Jump to a folder (used by the sidebar's pinned entries)."""
        if os.path.isdir(path):
            self.history.append(self.path)
            self.forward = []
            self._load(path)

    # ---- navigation ------------------------------------------------

    def _load(self, path):
        self.path = path
        try:
            names = os.listdir(path)
        except OSError as e:
            self.listbox.set_items([(tr("fm.error", e=e), "")]);
            self.path_label.text = path
            self.status.text = tr("fm.cannot_read", path=path)
            self.root.redraw()
            return
        dirs = []
        files = []
        for n in names:
            full = self._join(n)
            try:
                isdir = os.path.isdir(full)
            except OSError:
                isdir = False
            if isdir:
                dirs.append((n + "/", "<dir>"))
            else:
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    sz = 0
                files.append((n, _fmt_size(sz)))
        dirs.sort(key=lambda it: it[0].lower())
        files.sort(key=lambda it: it[0].lower())
        items = dirs + files
        if not items:
            items = [(tr("fm.empty"), "")]
        self.listbox.set_items(items)
        self.path_label.text = path
        self.status.text = tr("fm.items", n=len(names), path=path)
        self.root.redraw()
        kern.write("fm: %s (%d items)\n" % (path, len(names)))

    def refresh(self):
        self._load(self.path)

    def new_folder(self):
        """Create a uniquely named folder.  Only /home is writable, so when
        the current directory is read-only the folder is created there and
        the manager navigates to it (the user sees the new folder instead
        of a dead-end error)."""
        target = self.path
        jumped = False
        if not self._writable():
            target = "/home"
            jumped = True
        base = target.rstrip("/") + "/"
        name = tr("fm.new_folder")
        i = 2
        while os.path.exists(base + name):
            name = tr("fm.new_folder_n", n=i)
            i += 1
        try:
            os.mkdir(base + name)
        except OSError as e:
            self.status.text = tr("fm.mkdir_fail", p=base + name, e=e)
            self.root.redraw()
            return
        kern.write("fm: new folder %s\n" % (base + name))
        if jumped:
            self.history.append(self.path)
            self.forward = []
            self.path = target
            self.status.text = tr("fm.home_only", p=base + name)
        else:
            self.status.text = tr("fm.created", p=base + name)
        self.refresh()

    def delete_selected(self):
        """Delete the selected entry (file, or empty folder); /home only."""
        sel = self._selected_name()
        if sel is None:
            return
        full = self._join(sel)
        if not full.startswith("/home"):
            self.status.text = tr("fm.read_only")
            self.root.redraw()
            return
        try:
            isdir = os.path.isdir(full)
            if isdir:
                os.rmdir(full)
            else:
                os.remove(full)
        except OSError as e:
            self.status.text = tr("fm.delete_fail", p=sel, e=e)
            self.root.redraw()
            return
        kern.write("fm: deleted %s\n" % full)
        self.status.text = tr("fm.deleted", p=full)
        self.refresh()

    def go_up(self):
        p = self._parent(self.path)
        if p != self.path:
            self.history.append(self.path)
            self.forward = []
            self._load(p)

    def go_home(self):
        if self.path != "/":
            self.history.append(self.path)
            self.forward = []
            self._load("/")

    def go_back(self):
        if self.history:
            self.forward.append(self.path)
            self._load(self.history.pop())

    def go_forward(self):
        if self.forward:
            self.history.append(self.path)
            self._load(self.forward.pop())

    def _selected_name(self):
        it = self.listbox.get()
        if it is None:
            return None
        label = it[0] if isinstance(it, (tuple, list)) else it
        return label.rstrip("/")

    def activate(self, index, item):
        label = item[0] if isinstance(item, (tuple, list)) else item
        if label == "(empty)":
            return
        isdir = label.endswith("/")
        name = label.rstrip("/")
        full = self._join(name)
        kern.write("fm: activate %s (%s)\n" % (full, "dir" if isdir else "file"))
        if isdir:
            self.history.append(self.path)
            self.forward = []
            self._load(full)
        else:
            low = name.lower()
            # double-click uses the FIRST configured app for the extension
            apps = appbar.open_with_for(name)
            if not apps:
                apps = ["editor"] if low.endswith(_EDITABLE) else []
            if apps and apps[0] == "run" and low.endswith(".py"):
                try:
                    self.run_selected()
                except Exception as e:
                    self.status.text = tr("fm.open_fail", p=full, e=e)
                    self.root.redraw()
            elif low.endswith(_EDITABLE):
                try:
                    self.open_editor(full)
                except Exception as e:
                    self.status.text = tr("fm.open_fail", p=full, e=e)
                    self.root.redraw()
            elif low.endswith(_IMAGE):
                try:
                    self.open_image(full)
                except Exception as e:
                    self.status.text = tr("fm.view_fail", p=full, e=e)
                    self.root.redraw()
            elif low.endswith(_MEDIA):
                try:
                    self.open_media(full)
                except Exception as e:
                    self.status.text = tr("fm.play_fail", p=full, e=e)
                    self.root.redraw()
            elif low.endswith(_ARCHIVE):
                try:
                    self.open_archive(full)
                except Exception as e:
                    self.status.text = tr("fm.archive_fail", p=full, e=e)
                    self.root.redraw()
            else:
                self.status.text = tr("fm.dbl_open",
                    p=full, s=_fmt_size(os.path.getsize(full)))
                self.root.redraw()

    # ---- run / open ---------------------------------------------------

    def run_selected(self):
        """Run the selected .py file in a fresh namespace; stdout is
        captured into the status bar and echoed to the serial console."""
        sel = self._selected_name()
        if sel is None:
            self.status.text = tr("fm.select_py")
            self.root.redraw()
            return
        full = self._join(sel)
        if not os.path.isfile(full):
            self.status.text = tr("fm.not_file", p=full)
            self.root.redraw()
            return
        if not full.lower().endswith(".py"):
            self.status.text = tr("fm.run_py_only", p=full)
            self.root.redraw()
            return
        kern.write("fm: run %s\n" % full)
        out = []
        try:
            with open(full, "r") as f:
                src = f.read()
            code = compile(src, full, "exec")
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = _OutSink(out)
            sys.stderr = _OutSink(out)
            try:
                ns = {"__name__": "__main__"}
                exec(code, ns)
            except SystemExit:
                pass
            except Exception:
                out.append(traceback.format_exc())
            finally:
                sys.stdout, sys.stderr = old_out, old_err
        except Exception as e:
            out.append("%r" % e)
        text = "".join(out)
        for ln in text.splitlines():
            kern.write("fm: run out: %s\n" % ln)
        self.status.text = (text if text else tr("fm.ran_no_out", p=full)
                            ).replace("\n", " | ")[-200:]
        self.root.redraw()

    def open_editor(self, path=None):
        sel = self._selected_name()
        if path is None and sel is not None:
            p = self._join(sel)
            if os.path.isfile(p):
                path = p
        kern.write("fm: opening editor on %s\n" % (path or "<new>"))
        import editor

        def back():
            # the editor is quitting: fm takes the screen back
            import tk as tkmod
            tkmod._ACTIVE_ROOT = self.root
            self.refresh()
            self.root.activate()

        # editor.run restores OUR keyboard (self.root.kb) when it quits
        editor.run(self.root.fb, self.root.kb, self.root.mouse,
                   on_quit=back, path=path)

    def open_image(self, path=None):
        sel = self._selected_name()
        if path is None and sel is not None:
            p = self._join(sel)
            if os.path.isfile(p):
                path = p
        kern.write("fm: opening imgview on %s\n" % (path or "<none>"))
        import imgview

        def back():
            # the viewer is quitting: fm takes the screen back
            import tk as tkmod
            tkmod._ACTIVE_ROOT = self.root
            self.refresh()
            self.root.activate()

        imgview.run(self.root.fb, self.root.kb, self.root.mouse,
                    on_quit=back, path=path)

    def open_media(self, path=None):
        sel = self._selected_name()
        if path is None and sel is not None:
            p = self._join(sel)
            if os.path.isfile(p):
                path = p
        kern.write("fm: opening mplayer on %s\n" % (path or "<none>"))
        import mplayer

        def back():
            # the player is quitting: fm takes the screen back
            import tk as tkmod
            tkmod._ACTIVE_ROOT = self.root
            self.refresh()
            self.root.activate()

        mplayer.run(self.root.fb, self.root.kb, self.root.mouse,
                    on_quit=back, path=path)

    def open_imgedit(self, path=None):
        sel = self._selected_name()
        if path is None and sel is not None:
            p = self._join(sel)
            if os.path.isfile(p):
                path = p
        kern.write("fm: opening imgedit on %s\n" % (path or "<new>"))
        import imgedit

        def back():
            import tk as tkmod
            tkmod._ACTIVE_ROOT = self.root
            self.refresh()
            self.root.activate()

        imgedit.run(self.root.fb, self.root.kb, self.root.mouse,
                    on_quit=back, path=path)

    def open_archive(self, path=None):
        """Open a zip/7z/tar/gz/bz2/xz in the archive viewer (list +
        extract to /home)."""
        sel = self._selected_name()
        if path is None and sel is not None:
            p = self._join(sel)
            if os.path.isfile(p):
                path = p
        kern.write("fm: opening archive viewer on %s\n" % (path or "<none>"))
        import arc

        def back():
            # the viewer is quitting: fm takes the screen back
            import tk as tkmod
            tkmod._ACTIVE_ROOT = self.root
            self.refresh()
            self.root.activate()

        arc.run(self.root.fb, self.root.kb, self.root.mouse,
                on_quit=back, path=path)

    def quit_app(self):
        kern.write("fm: quit\n")
        self.root.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    """Start the file manager.  kb is the CALLER keyboard (the shell's);
    it is restored when the manager quits.  path is the starting folder.
    Returns immediately (the kernel event loop drives the UI)."""
    kb2 = Keyboard()
    root = tk.Tk(title="Ripos File Manager", sidebar=True)
    # start BEFORE building the tree: pack() redraws, and the lazy _ensure
    # must not create a second Keyboard (it would steal kern.on_key from kb2)
    root.start(fb, kb2, mouse)
    # app window system: title-bar '-'/'X' + left sidebar; pinned folders
    # navigate this manager
    root._prev_kb = kb
    root._back = on_quit
    root.begin_build()          # one repaint at the end, not 20+
    app = FileManager(root, on_quit, start=path)
    root._pinned_handler = app.navigate
    root.end_build()

    def quit_app():
        # restore the caller keyboard FIRST so the shell is live even if
        # the redraw (on_quit) is slow; then destroy + repaint
        kb.activate()
        app.quit_app()

    root._close_handler = quit_app
    app.btn_quit.command = quit_app
    root.bind("<Escape>", lambda e: quit_app())
    app.refresh()
    kern.write("fm: file manager ready -- mouse + keyboard\n")
    root.mainloop()
    kern.write("fm: run returned (event-driven)\n")
