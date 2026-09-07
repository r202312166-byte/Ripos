# arc.py -- archive viewer for Ripos (M9.3).
#
# A full-screen tkinter app for zip / 7z / tar / gz / bz2 / xz archives:
# the entries are listed with sizes, Enter or a double-click extracts the
# selected entry into /home (the writable RAM disk), 'e' extracts the whole
# archive, Esc quits back to the caller (shell / file manager).  Opened
# from the shell with:  arc [path]   or by double-clicking an archive in
# the file manager.

import io
import kern
import os
import text
import zipfile
import tarfile
import tkinter as tk
from keyboard import Keyboard

TITLE_BG = (70, 95, 140)
TITLE_TEXT = (255, 255, 255)
STATUS_FG = (150, 165, 180)


def _fmt_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / (1024.0 * 1024.0))
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


class Archive:
    """A uniform view over zip / 7z / tar (+gz/bz2/xz): namelist() and
    read(name) -> bytes.  Extraction always lands under /home so it works
    on the writable RAM disk."""

    def __init__(self, path):
        self.path = path
        self.kind = "archive"
        with open(path, "rb") as f:
            data = f.read()
        if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06") \
                or data.startswith(b"PK\x07\x08"):
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            self.kind = "zip"
        elif data.startswith(b"7z\xbc\xaf\x27\x1c"):
            import sevenz
            self._sz = sevenz.SevenZipFile(data)
            self.kind = "7z"
        else:
            self._tar = tarfile.open(fileobj=io.BytesIO(data))
            self.kind = "tar"
        self.names = self._namelist()

    def _namelist(self):
        if self.kind == "zip":
            out = []
            for i in self._zip.infolist():
                out.append((i.filename, i.file_size,
                            "dir" if i.is_dir() else "file"))
            return out
        if self.kind == "7z":
            out = []
            for n in self._sz.namelist():
                if n.endswith("/"):
                    out.append((n, 0, "dir"))
                else:
                    try:
                        sz = len(self._sz.read(n))
                    except Exception:
                        sz = 0
                    out.append((n, sz, "file"))
            return out
        out = []
        for m in self._tar.getmembers():
            out.append((m.name, m.size,
                        "dir" if m.isdir() else "file"))
        return out

    def read(self, name):
        if self.kind == "zip":
            return self._zip.read(name)
        if self.kind == "7z":
            return self._sz.read(name)
        m = self._tar.getmember(name)
        f = self._tar.extractfile(m)
        if f is None:
            return b""
        return f.read()

    def close(self):
        try:
            if self.kind == "zip":
                self._zip.close()
            elif self.kind == "tar":
                self._tar.close()
        except Exception:
            pass


class ArcViewer:
    def __init__(self, root, on_quit, path=None):
        self.root = root
        self.on_quit = on_quit
        self.arch = None
        self.path = path
        self._build()
        if path:
            self.load(path)

    # ---- UI --------------------------------------------------------

    def _build(self):
        r = self.root
        bar = tk.Frame(r, bg=(30, 36, 48))
        self.btn_extract = tk.Button(bar, text="Extract",
                                     command=self.extract_selected)
        self.btn_all = tk.Button(bar, text="Extract All",
                                 command=self.extract_all)
        self.btn_quit = tk.Button(bar, text="Quit", command=self.quit_app)
        for b in (self.btn_extract, self.btn_all, self.btn_quit):
            b.pack(side="left", padx=2, pady=2)
        bar.pack(side="top", fill="x")

        self.path_label = tk.Label(r, text="", fg=(200, 210, 230),
                                   bg=(26, 32, 42), padx=6, pady=2)
        self.path_label.pack(side="top", fill="x")

        self.listbox = tk.Listbox(r, height=24, on_activate=self.activate)
        self.listbox.pack(fill="both", expand=True, padx=4, pady=4)
        self.listbox.focus_set()

        self.status = tk.Label(r, text="", fg=STATUS_FG, bg=(22, 26, 34),
                               padx=6, pady=2)
        self.status.pack(side="bottom", fill="x")

        r.bind("<BackSpace>", lambda e: self.quit_app())
        r.bind("<e>", lambda e: self.extract_all())
        r.bind("<E>", lambda e: self.extract_all())

    def load(self, path):
        try:
            self.arch = Archive(path)
        except Exception as e:
            self.status.text = "open %s: %r" % (path, e)
            self.path_label.text = path
            self.root.redraw()
            kern.write("arc: open %s failed: %r\n" % (path, e))
            return
        items = []
        for name, size, kind in self.arch.names:
            if kind == "dir":
                items.append((name.rstrip("/") + "/", "<dir>"))
            else:
                items.append((name, _fmt_size(size)))
        if not items:
            items = [("(empty archive)", "")]
        self.listbox.set_items(items)
        self.path_label.text = "%s  [%s, %d entries]" % (
            path, self.arch.kind, len(self.arch.names))
        self.status.text = "Enter/double-click extracts to /home; E extracts all"
        self.root.redraw()
        kern.write("arc: %s (%d entries)\n" % (path, len(self.arch.names)))

    def _selected_name(self):
        it = self.listbox.get()
        if it is None:
            return None
        label = it[0] if isinstance(it, (tuple, list)) else it
        if label == "(empty archive)":
            return None
        return label.rstrip("/")

    def _out_dir(self, name):
        """Extraction root for one entry: /home/<archive base>/<parent>."""
        base = os.path.basename(self.path)
        for suf in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip",
                    ".7z", ".tar", ".gz", ".bz2", ".xz"):
            if base.lower().endswith(suf):
                base = base[:-len(suf)]
                break
        parent = os.path.dirname(name)
        return os.path.join("/home", base or "arc", parent)

    def extract_selected(self):
        name = self._selected_name()
        if name is None or self.arch is None:
            return
        try:
            d = self._out_dir(name)
            os.makedirs(d, exist_ok=True)
            data = self.arch.read(name)
            target = os.path.join(d, os.path.basename(name))
            with open(target, "wb") as f:
                f.write(data)
            kern.write("arc: extracted %s (%d bytes)\n" % (target, len(data)))
            self.status.text = "extracted %s (%s)" % (target, _fmt_size(len(data)))
        except Exception as e:
            kern.write("arc: extract %s failed: %r\n" % (name, e))
            self.status.text = "extract %s: %r" % (name, e)
        self.root.redraw()

    def extract_all(self):
        if self.arch is None:
            return
        out = 0
        root_out = os.path.join("/home", os.path.basename(self.path)
                                .rsplit(".", 1)[0] or "arc")
        try:
            for name, _size, kind in self.arch.names:
                if kind == "dir":
                    continue
                data = self.arch.read(name)
                target = os.path.join(root_out, name)
                d = os.path.dirname(target)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
                out += 1
            kern.write("arc: extracted %d files to %s\n" % (out, root_out))
            self.status.text = "extracted %d files to %s" % (out, root_out)
        except Exception as e:
            kern.write("arc: extract all failed: %r\n" % (e,))
            self.status.text = "extract all: %r" % (e,)
        self.root.redraw()

    def activate(self, index, item):
        self.extract_selected()

    def quit_app(self):
        if self.arch is not None:
            try:
                self.arch.close()
            except Exception:
                pass
            self.arch = None
        kern.write("arc: quit\n")
        self.root.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    """Start the archive viewer.  kb is the CALLER keyboard (restored on
    quit); returns immediately (the kernel event loop drives the UI)."""
    kb2 = Keyboard()
    root = tk.Tk(title="Ripos Archive Viewer", sidebar=True)
    root.start(fb, kb2, mouse)
    root.begin_build()          # one repaint at the end, not 20+
    app = ArcViewer(root, on_quit, path)
    # app window system: title-bar '-'/'X' + left sidebar
    root._prev_kb = kb
    root._back = on_quit
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
        app.status.text = "arc: pass a path, e.g. arc /home/demo.zip"
        kern.write("arc: no path given\n")
    kern.write("arc: archive viewer ready -- mouse + keyboard\n")
    root.mainloop()
    kern.write("arc: run returned (event-driven)\n")
