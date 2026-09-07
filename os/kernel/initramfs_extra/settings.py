"""settings.py -- user configuration for Ripos (themes, language, sidebars).

Config is persisted in /home/settings.json (the writable RAM disk):

    {
      "theme": "dark" | "light",
      "language": "en" | "zh",
      "pinned": ["/apps", "/home", "/"],
      "sidebar_left": ["pinned", "apps"],   # sections of the left sidebar
      "sidebar_right": "none" | "pinned" | "apps",
    }

The config app (config.py, shell command 'config') edits these fields;
theme()/c() give the UI modules their colors and apply_theme() re-colors
the already-running shell, editor and file manager surfaces.
"""

import json

DEFAULTS = {
    "theme": "dark",
    "language": "en",
    "pinned": ["/apps", "/home", "/"],
    "sidebar_left": ["pinned", "apps"],
    "sidebar_right": "none",
}

_config = None


def load():
    global _config
    if _config is not None:
        return _config
    _config = dict(DEFAULTS)
    try:
        with open("/home/settings.json") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in ("theme", "language", "sidebar_left", "sidebar_right"):
                if k in data:
                    _config[k] = data[k]
            if isinstance(data.get("pinned"), list):
                p = [str(x) for x in data["pinned"]
                     if isinstance(x, str) and x.startswith("/")]
                if p:
                    _config["pinned"] = p
    except Exception:
        pass
    return _config


def save():
    try:
        with open("/home/settings.json", "w") as f:
            json.dump(load(), f)
    except Exception:
        pass


def get(key, default=None):
    return load().get(key, default)


def set(key, value):
    load()[key] = value
    save()


# ---------------------------------------------------------------------------
# themes
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "bg": (22, 26, 34),           # window background
        "fg": (225, 230, 238),        # default text
        "panel": (40, 46, 58),        # widget background
        "panel_fg": (235, 240, 245),
        "input_bg": (40, 46, 58),
        "title_bg": (70, 95, 140),
        "title_fg": (255, 255, 255),
        "accent": (210, 130, 40),     # focus ring / highlights
        "sel": (70, 95, 140),         # listbox selection
        "sidebar_bg": (24, 28, 38),
        "sidebar_fg": (210, 220, 235),
        "sidebar_head": (140, 160, 185),
        "sidebar_pin": (48, 62, 88),
        "sidebar_app": (40, 52, 70),
        "sidebar_edge": (110, 125, 150),
        "chrome": (60, 80, 120),
        "chrome_close": (190, 64, 64),
        "chrome_edge": (180, 195, 215),
        "bar": (30, 36, 48),          # toolbar
        "status": (22, 26, 34),
        "status_fg": (150, 165, 180),
        "cursor": (150, 190, 235),
        "sel_bg": (60, 88, 128),      # editor selection
        "err": (235, 110, 110),
        "ok": (150, 200, 150),
        "prompt": (120, 200, 120),
        "canvas_bg": (30, 36, 48),
        "label_bg": (40, 46, 58),
        "focus": (210, 130, 40),
    },
    "light": {
        "bg": (255, 255, 255),        # white background
        "fg": (0, 0, 0),              # black text
        "panel": (255, 248, 210),     # light yellow panels
        "panel_fg": (0, 0, 0),
        "input_bg": (255, 250, 225),
        "title_bg": (255, 240, 170),  # light yellow title bars
        "title_fg": (0, 0, 0),
        "accent": (200, 150, 20),     # darker yellow for contrast
        "sel": (255, 235, 130),
        "sidebar_bg": (255, 246, 200),
        "sidebar_fg": (0, 0, 0),
        "sidebar_head": (120, 90, 0),
        "sidebar_pin": (255, 240, 170),
        "sidebar_app": (240, 228, 170),
        "sidebar_edge": (160, 140, 60),
        "chrome": (220, 190, 90),
        "chrome_close": (220, 100, 90),
        "chrome_edge": (120, 100, 30),
        "bar": (255, 240, 170),
        "status": (250, 244, 220),
        "status_fg": (80, 70, 30),
        "cursor": (60, 80, 140),
        "sel_bg": (255, 235, 130),
        "err": (200, 40, 40),
        "ok": (30, 120, 60),
        "prompt": (20, 120, 40),
        "canvas_bg": (255, 250, 225),
        "label_bg": (255, 248, 210),
        "focus": (200, 150, 20),
    },
}


def theme():
    return THEMES.get(get("theme", "dark"), THEMES["dark"])


def c(key, fallback=None):
    """The current theme color for a role, with a dark-theme fallback."""
    t = theme()
    if key in t:
        return t[key]
    return fallback if fallback is not None else THEMES["dark"].get(key, (0, 0, 0))


def set_theme(name):
    if name in THEMES:
        set("theme", name)
        apply_theme()


def apply_theme():
    """Recolor the module-level constants of the running UI surfaces so an
    existing shell / editor / fm pick up the new theme on their next draw.
    """
    t = theme()
    try:
        import appbar
        appbar.SIDEBAR_BG = t["sidebar_bg"]
        appbar.SIDEBAR_FG = t["sidebar_fg"]
        appbar.SIDEBAR_HEAD = t["sidebar_head"]
        appbar.SIDEBAR_PIN_BG = t["sidebar_pin"]
        appbar.SIDEBAR_APP_BG = t["sidebar_app"]
        appbar.SIDEBAR_EDGE = t["sidebar_edge"]
        appbar.CHROME_BG = t["chrome"]
        appbar.CLOSE_BG = t["chrome_close"]
        appbar.CHROME_EDGE = t["chrome_edge"]
    except Exception:
        pass
    try:
        import repl
        repl.BG = t["bg"]
        repl.TEXT_COLOR = t["fg"]
        repl.TITLE = t["prompt"]
        repl.PROMPT_COLOR = t["prompt"]
        repl.ERROR_COLOR = t["err"]
        repl.CURSOR = t["cursor"]
    except Exception:
        pass
    try:
        import editor as _ed
        _ed.BG = t["bg"]
        _ed.FG = t["fg"]
        _ed.PROMPT = t["prompt"]
        _ed.ERROR = t["err"]
        _ed.INFO = t["status_fg"]
        _ed.OK = t["ok"]
        _ed.CURSOR = t["cursor"]
        _ed.SEL_BG = t["sel_bg"]
    except Exception:
        pass
    try:
        import fm as _fm
        _fm.TITLE_BG = t["title_bg"]
        _fm.PATH_BG = t["status"]
        _fm.STATUS_FG = t["status_fg"]
    except Exception:
        pass
    try:
        import wm
        wm.BG = t["bg"]
        wm.TITLE_BG = t["title_bg"]
        wm.TITLE_BG_FOCUS = t["accent"]
        wm.WIN_BG = t["panel"]
        wm.BORDER = t["sidebar_edge"]
        wm.TEXT = t["fg"]
        wm.TITLE_TEXT = t["title_fg"]
        wm.HINT_TEXT = t["status_fg"]
    except Exception:
        pass
