# config.py -- the Ripos settings app: theme (dark/light), language,
# the left/right sidebar content and the pinned folders.  Persists to
# /home/settings.json.  Boot it from the shell with:  config

import kern
import tkinter as tk
import appbar
import settings
from keyboard import Keyboard

L = {
    "en": {
        "title": "Ripos Settings",
        "theme": "Theme",
        "dark": "Dark",
        "light": "Light",
        "language": "Language",
        "lang_en": "English",
        "lang_zh": "Chinese",
        "sidebar": "Sidebar content",
        "left_pinned": "Left: pinned folders [%s]",
        "left_apps": "Left: hidden apps [%s]",
        "right": "Right sidebar",
        "right_none": "Right: none [%s]",
        "right_pinned": "Right: pinned [%s]",
        "right_apps": "Right: apps [%s]",
        "pinned": "Pinned folders",
        "add": "Add",
        "remove": "Remove",
        "hint": "type a path (e.g. /apps) + Add",
        "saved": "saved",
        "quit": "Quit",
    },
    "zh": {
        "title": "Ripos 设置",
        "theme": "主题",
        "dark": "深色",
        "light": "浅色",
        "language": "语言",
        "lang_en": "英文",
        "lang_zh": "中文",
        "sidebar": "侧边栏内容",
        "left_pinned": "左侧: 固定文件夹 [%s]",
        "left_apps": "左侧: 隐藏应用 [%s]",
        "right": "右侧边栏",
        "right_none": "右侧: 无 [%s]",
        "right_pinned": "右侧: 固定 [%s]",
        "right_apps": "右侧: 应用 [%s]",
        "pinned": "固定文件夹",
        "add": "添加",
        "remove": "删除",
        "hint": "输入路径 (如 /apps) 再按 添加",
        "saved": "已保存",
        "quit": "退出",
    },
}


def tr(key):
    lang = settings.get("language", "en")
    return L.get(lang, L["en"]).get(key, key)


class ConfigApp:
    def __init__(self, root, on_quit):
        self.root = root
        self.on_quit = on_quit
        self.pinned_entry = None
        self._build()

    # ---- UI ----------------------------------------------------------

    def _build(self):
        r = self.root
        bar = tk.Frame(r, bg=settings.c('bar'))
        tk.Button(bar, text=tr("quit"), command=self.quit_app).pack(
            side="left", padx=2, pady=2)
        bar.pack(side="top", fill="x")

        LABEL_W = 240

        def section(title):
            tk.Label(r, text=title, fg=settings.c('title_fg'),
                     bg=settings.c('title_bg'), padx=8, pady=3).pack(
                side="top", fill="x")

        def row(label, *make_widgets):
            """One aligned settings row: a fixed-width label column on the
            left, the controls on the right.  Widgets are created INSIDE the
            row frame (their master must be the frame, not the root, or the
            pack machinery stacks them at the root and the layout steps)."""
            f = tk.Frame(r, bg=settings.c('panel'))
            col = tk.Frame(f, width=LABEL_W, bg=settings.c('panel'))
            tk.Label(col, text=label, fg=settings.c('panel_fg'),
                     bg=settings.c('panel'), padx=6, pady=3, anchor='w'
                     ).pack(side="left", fill="y")
            col.pack(side="left", fill="y")
            for make in make_widgets:
                make(f).pack(side="left", padx=4, pady=3)
            f.pack(side="top", fill="x", padx=8, pady=2)

        # theme
        section(tr("theme"))
        cur = settings.get("theme", "dark")
        row(tr("theme"),
            lambda f: tk.Button(f, text=tr("dark"),
                                command=lambda: self._set_theme("dark")),
            lambda f: tk.Button(f, text=tr("light"),
                                command=lambda: self._set_theme("light")),
            lambda f: tk.Label(f, text=("[%s]" % cur),
                               fg=settings.c('panel_fg'),
                               bg=settings.c('panel')))

        # language
        section(tr("language"))
        lang = settings.get("language", "en")
        row(tr("language"),
            lambda f: tk.Button(f, text=tr("lang_en"),
                                command=lambda: self._set_lang("en")),
            lambda f: tk.Button(f, text=tr("lang_zh"),
                                command=lambda: self._set_lang("zh")),
            lambda f: tk.Label(f, text=("[%s]" % lang),
                               fg=settings.c('panel_fg'),
                               bg=settings.c('panel')))

        # left sidebar sections
        section(tr("sidebar"))
        left = settings.get("sidebar_left", ["pinned", "apps"])
        row(tr("left_pinned") % ("ON" if "pinned" in left else "off"),
            lambda f: tk.Button(f, text=tr("pinned"),
                                command=lambda: self._toggle_left("pinned")))
        row(tr("left_apps") % ("ON" if "apps" in left else "off"),
            lambda f: tk.Button(f, text=tr("apps"),
                                command=lambda: self._toggle_left("apps")))

        # right sidebar
        section(tr("right"))
        right = settings.get("sidebar_right", "none")
        for key, val in (("right_none", "none"),
                         ("right_pinned", "pinned"),
                         ("right_apps", "apps")):
            row(tr(key) % ("ON" if right == val else "off"),
                lambda f, v=val: tk.Button(f, text=v,
                                           command=lambda v=v: self._set_right(v)))
        self.pinned_list = tk.Listbox(r, height=6,
                                      on_select=lambda: None)
        self.pinned_list.pack(side="top", fill="x", padx=8, pady=2)
        self._refresh_pinned()
        ef = tk.Frame(r, bg=settings.c('panel'))
        tk.Label(ef, text=tr("pinned"), fg=settings.c('panel_fg'),
                 bg=settings.c('panel'), padx=6, pady=3).pack(
            side="left")
        self.pinned_entry = tk.Entry(ef, width=24)
        self.pinned_entry.pack(side="left", padx=4, pady=2)
        tk.Button(ef, text=tr("add"),
                  command=self._add_pinned).pack(side="left", padx=2)
        tk.Button(ef, text=tr("remove"),
                  command=self._remove_pinned).pack(side="left", padx=2)
        ef.pack(side="top", fill="x", padx=8)
        tk.Label(r, text=tr("hint"), fg=settings.c('status_fg'),
                 bg=settings.c('bg'), padx=8, pady=2).pack(side="top",
                                                           fill="x")
        self.status = tk.Label(r, text="", fg=settings.c('fg'),
                               bg=settings.c('bg'), padx=6, pady=2)
        self.status.pack(side="bottom", fill="x")

    def _rebuild(self):
        # clear the tree and rebuild (theme/language changed the labels
        # and colors) -- one repaint, not one per widget
        self.root.begin_build()
        for w in list(self.root.children):
            try:
                self.root.children.remove(w)
            except ValueError:
                pass
        self._build()
        self.root.end_build()

    def _refresh_pinned(self):
        if self.pinned_list is not None:
            self.pinned_list.set_items([(p, "") for p in appbar.PINNED])

    # ---- actions ------------------------------------------------------

    def _set_theme(self, name):
        settings.set_theme(name)
        appbar._T = settings.theme()
        self.status.text = tr("saved") + ": " + name
        self._rebuild()

    def _set_lang(self, lang):
        settings.set("language", lang)
        self.status.text = tr("saved")
        self._rebuild()

    def _toggle_left(self, section):
        left = list(settings.get("sidebar_left", ["pinned", "apps"]))
        if section in left:
            left.remove(section)
        else:
            left.append(section)
        settings.set("sidebar_left", left)
        self._rebuild()

    def _set_right(self, val):
        settings.set("sidebar_right", val)
        self._rebuild()

    def _add_pinned(self):
        p = self.pinned_entry.get().strip()
        if not p.startswith("/"):
            self.status.text = "path must start with /"
            self.root.redraw()
            return
        if p not in appbar.PINNED:
            appbar.PINNED.append(p)
            settings.set("pinned", list(appbar.PINNED))
            self._refresh_pinned()
        self.pinned_entry.set("")
        self.status.text = tr("saved")
        self.root.redraw()

    def _remove_pinned(self):
        it = self.pinned_list.get()
        if it is None:
            return
        label = it[0] if isinstance(it, (tuple, list)) else it
        if label in appbar.PINNED:
            appbar.PINNED.remove(label)
            settings.set("pinned", list(appbar.PINNED))
            self._refresh_pinned()
            self.root.redraw()

    def quit_app(self):
        kern.write("config: quit\n")
        self.root.destroy()
        if self.on_quit is not None:
            self.on_quit()


def run(fb, kb, mouse, on_quit=None, path=None):
    kb2 = Keyboard()
    root = tk.Tk(title=tr("title"), sidebar=True)
    root.start(fb, kb2, mouse)
    root._prev_kb = kb
    root._back = on_quit
    root.begin_build()          # one repaint at the end, not 20+
    app = ConfigApp(root, on_quit)
    root.end_build()

    def quit_app():
        kb.activate()
        app.quit_app()

    root._close_handler = quit_app
    root.bind("<Escape>", lambda e: quit_app())
    kern.write("config: settings app ready\n")
    root.mainloop()
    kern.write("config: run returned (event-driven)\n")
