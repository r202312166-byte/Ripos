"""appbar.py -- the Ripos app window system: title-bar chrome + left sidebar.

Every full-screen app (a tk.Tk root) and the M8 shell share a window
chrome: a title bar with "-" (minimize) and "X" (close) buttons, and a
left sidebar listing pinned folders and hidden (minimized) running
apps.  The chrome and sidebar are drawn by the OWNER of the screen --
the tk root or the shell -- through draw_chrome()/draw_sidebar(); this
module only holds the shared geometry, the app registry, the
hit-testing and the user settings, so nothing has to be added to the
apps themselves.

Hidden apps keep running (their keyboard handler is parked); clicking
their sidebar entry restores them.  Pinned folders open the file
manager at that folder.

User settings live in /home/settings.json (the writable RAM disk):

    {
      "pinned": ["/apps", "/home", "/"],
      "open_with": {
        ".py": ["editor", "run"],
        ".png": ["imgview", "editor"],
        ...
      }
    }

The file manager's "Open" button uses open_with_for() to list the apps
that can open the selected file.
"""

import json
import kern
import text as textmod
import settings
import i18n

# Optional: the shell registers its redraw here (boot.py); when an app
# quits WITHOUT a back() callback of its own (e.g. 'import tkdemo' +
# Quit), the window system redraws the shell so the screen is not stuck.
_fallback_redraw = None


def set_fallback(fn):
    global _fallback_redraw
    _fallback_redraw = fn

# geometry (BTN_H matches wm.TITLE_H = 16 so the buttons fill the bar)
SIDEBAR_W = 92
BTN_W = 22
BTN_H = 16

# palette (from the current theme; settings.apply_theme() re-colors a
# running shell/app on theme switch)
_T = settings.theme()
SIDEBAR_BG = _T["sidebar_bg"]
SIDEBAR_FG = _T["sidebar_fg"]
SIDEBAR_HEAD = _T["sidebar_head"]
SIDEBAR_PIN_BG = _T["sidebar_pin"]
SIDEBAR_APP_BG = _T["sidebar_app"]
SIDEBAR_EDGE = _T["sidebar_edge"]
CHROME_BG = _T["chrome"]
CLOSE_BG = _T["chrome_close"]
CHROME_EDGE = _T["chrome_edge"]

# ---- user settings (from /home/settings.json, fall back to defaults) ----

DEFAULT_PINNED = ["/apps", "/home", "/"]
DEFAULT_OPEN_WITH = {
    ".py": ["editor", "run"],
    ".txt": ["editor"],
    ".md": ["editor"],
    ".json": ["editor"],
    ".toml": ["editor"],
    ".cfg": ["editor"],
    ".ini": ["editor"],
    ".c": ["editor"],
    ".rs": ["editor"],
    ".h": ["editor"],
    ".rtf": ["editor"],
    ".docx": ["editor"],
    ".png": ["imgview", "editor"],
    ".jpg": ["imgview", "editor"],
    ".jpeg": ["imgview", "editor"],
    ".zip": ["arc", "editor"],
    ".7z": ["arc"],
    ".tar": ["arc"],
    ".tgz": ["arc"],
    ".gz": ["arc"],
    ".bz2": ["arc"],
    ".xz": ["arc"],
    ".mp3": ["mplayer"],
    ".mp4": ["mplayer"],
    ".wav": ["mplayer"],
    ".aac": ["mplayer"],
}

PINNED = list(DEFAULT_PINNED)
OPEN_WITH = dict(DEFAULT_OPEN_WITH)
_settings_loaded = False


def _ensure_settings():
    """One-time sync: the pinned-folders list comes from the shared
    settings module (the config app edits it); the open-with map stays
    local (read from /home/settings.json if present)."""
    global _settings_loaded
    if not _settings_loaded:
        _settings_loaded = True
        try:
            p = [str(x) for x in settings.get('pinned', DEFAULT_PINNED)
                 if str(x).startswith('/')]
            if p:
                PINNED[:] = p
        except Exception:
            pass
        try:
            with open("/home/settings.json") as f:
                cfg = json.load(f)
            if isinstance(cfg.get("open_with"), dict):
                # Merge saved choices over the built-in defaults PER KEY so
                # newer default mappings (.mp3/.mp4/.docx/.rtf/...) are never
                # wiped by an older saved map that predates them.
                ow = {}
                for k, v in cfg["open_with"].items():
                    if isinstance(v, list):
                        ow[str(k).lower()] = [str(a) for a in v]
                if ow:
                    OPEN_WITH.update(ow)
        except Exception:
            pass


def save_settings():
    """Write the current pinned/open_with settings back to /home."""
    try:
        with open("/home/settings.json", "w") as f:
            json.dump({"pinned": PINNED, "open_with": OPEN_WITH}, f)
        kern.write("appbar: settings saved to /home/settings.json\n")
    except Exception as e:
        kern.write("appbar: save settings failed: %s\n" % e)


def open_with_for(name):
    """The list of apps that can open a file (by its extension)."""
    _ensure_settings()
    try:
        dot = name.rfind(".")
        ext = name[dot:].lower() if dot >= 0 else ""
    except Exception:
        ext = ""
    return list(OPEN_WITH.get(ext, []))


# ---- app registry -----------------------------------------------------

APPS = []   # {"id", "title", "restore", "close", "hidden"}


def register(title, restore, close=None):
    """Register a running app.  restore() is called when the app is
    un-minimized from the sidebar; close() closes it for good.  Returns
    the app id."""
    app_id = len(APPS) + 1
    APPS.append({"id": app_id, "title": title, "restore": restore,
                 "close": close, "hidden": False})
    return app_id


def find(app_id):
    for a in APPS:
        if a["id"] == app_id:
            return a
    return None


def unregister(app_id):
    """Drop a destroyed app from the registry.  The entries hold bound
    methods (restore/close) that reference the app object, so leaving them
    in place leaks the whole app -- including big payloads like the media
    player's decoded PCM -- until the next app happens to reuse the slot."""
    for i, a in enumerate(APPS):
        if a["id"] == app_id:
            del APPS[i]
            return True
    return False


def hide(app_id):
    a = find(app_id)
    if a is not None:
        a["hidden"] = True


def unhide(app_id):
    a = find(app_id)
    if a is not None:
        a["hidden"] = False


def hidden_apps():
    return [a for a in APPS if a["hidden"]]


def restore(app_id):
    """Bring a minimized app back (clicked in the sidebar)."""
    a = find(app_id)
    if a is None or not a["hidden"]:
        return False
    try:
        a["restore"]()
    except Exception as e:
        kern.write("appbar: restore %s failed: %s\n" % (a["title"], e))
        return False
    a["hidden"] = False
    return True


# ---- sidebar geometry + hit-testing ------------------------------------

SIDEBAR_R_W = 92   # the optional right sidebar strip (same width)


def _left_sections():
    """Which groups the LEFT sidebar shows (settings.sidebar_left)."""
    secs = settings.get("sidebar_left", ["pinned", "apps"])
    return [s for s in ("pinned", "apps") if s in secs]


def _right_sections():
    """Which groups the RIGHT sidebar shows (settings.sidebar_right)."""
    mode = settings.get("sidebar_right", "none")
    return {"pinned": ["pinned"], "apps": ["apps"]}.get(mode, [])


def right_sidebar_w():
    """Width of the configured right sidebar (0 when disabled)."""
    return SIDEBAR_R_W if _right_sections() else 0


def _rows_for(h, y0, pinned, apps, sections):
    """Sidebar rows: (kind, arg, label, y).  `sections` selects the
    groups: "pinned" = the pinned-folder list, "apps" = the hidden
    running apps.  h = strip height, y0 = strip top."""
    rows = []
    y = y0 + 4

    def section(title):
        nonlocal y
        rows.append(("head", None, title, y))
        y += textmod.LINE_H + 2

    if "pinned" in sections:
        section(i18n.tr("appbar.apps"))
        for i, p in enumerate(pinned):
            rows.append(("pin", i, p, y))
            y += textmod.LINE_H
    if "apps" in sections:
        hid = apps
        if hid and y + textmod.LINE_H + 8 < y0 + h:
            y += 8
            section(i18n.tr("appbar.running"))
            for a in hid:
                rows.append(("app", a["id"], a["title"], y))
                y += textmod.LINE_H
    return rows


def _entries(h, y0):
    return _rows_for(h, y0, PINNED, hidden_apps(), _left_sections())


def _draw_strip(fb, x0, y0, w, rows):
    fb.fill_rect(x0, y0, w, fb.height - y0, *SIDEBAR_BG)
    fb.fill_rect(x0 + w - 1, y0, 1, fb.height - y0, *SIDEBAR_EDGE)
    for kind, arg, label, y in rows:
        yy = y
        if yy + textmod.LINE_H > fb.height:
            break
        if kind == "head":
            textmod.draw_text(fb, x0 + 6, yy, label, SIDEBAR_HEAD)
        elif kind == "pin":
            fb.fill_rect(x0 + 2, yy, w - 5, textmod.LINE_H, *SIDEBAR_PIN_BG)
            textmod.draw_text(fb, x0 + 6, yy, label, SIDEBAR_FG)
        else:
            fb.fill_rect(x0 + 2, yy, w - 5, textmod.LINE_H, *SIDEBAR_APP_BG)
            textmod.draw_text(fb, x0 + 6, yy, label, SIDEBAR_FG)


def draw_sidebar(fb, y0=0):
    """Draw the left sidebar strip (x 0..SIDEBAR_W) on fb."""
    _ensure_settings()
    _draw_strip(fb, 0, y0, SIDEBAR_W, _entries(fb.height - y0, y0))


def draw_sidebar_right(fb, y0=0):
    """Draw the right sidebar strip at the right edge (content per
    settings.sidebar_right: none | pinned | apps)."""
    _ensure_settings()
    w = right_sidebar_w()
    if w <= 0:
        return
    x0 = fb.width - w
    rows = _rows_for(fb.height - y0, y0, PINNED, hidden_apps(),
                     _right_sections())
    _draw_strip(fb, x0, y0, w, rows)


def hit_sidebar(x, y, h, y0=0):
    """Map a click inside the LEFT sidebar strip to an action:
    ("pin", index) | ("app", app_id) | ("head", None) | None."""
    if x < 0 or x >= SIDEBAR_W or y < y0 or y >= y0 + h:
        return None
    for kind, arg, label, yy in _entries(h, y0):
        if yy <= y < yy + textmod.LINE_H:
            if kind in ("pin", "app"):
                return (kind, arg)
            return ("head", None)
    return None


def hit_sidebar_right(x, y, fbw, h, y0=0):
    """Map a click inside the RIGHT sidebar strip (if enabled)."""
    w = right_sidebar_w()
    if w <= 0 or x < fbw - w or y < y0 or y >= y0 + h:
        return None
    rows = _rows_for(h, y0, PINNED, hidden_apps(), _right_sections())
    for kind, arg, label, yy in rows:
        if yy <= y < yy + textmod.LINE_H:
            if kind in ("pin", "app"):
                return (kind, arg)
            return ("head", None)
    return None


# ---- title-bar chrome ---------------------------------------------------

def draw_chrome(fb, x0, y0, w):
    """Draw the '-' (minimize) and 'X' (close) buttons at the right end
    of a title bar strip (x0, y0, w, BTN_H).  Returns
    {'min': rect, 'close': rect} in framebuffer coordinates."""
    rects = {}
    bx = x0 + w - 2 * BTN_W - 8
    for i, (label, key, bg) in enumerate(
            (('-', 'min', CHROME_BG), ('X', 'close', CLOSE_BG))):
        rx, ry = bx + i * (BTN_W + 4), y0 + 2
        fb.fill_rect(rx, ry, BTN_W, BTN_H - 4, *bg)
        tw = textmod.text_width(label)
        textmod.draw_text(fb, rx + (BTN_W - tw) // 2, ry, label,
                          (255, 255, 255))
        fb.draw_rect(rx, ry, BTN_W, BTN_H - 4, *CHROME_EDGE)
        rects[key] = (rx, ry, BTN_W, BTN_H - 4)
    return rects


def hit_chrome(x, y, rects):
    """Which chrome button was clicked: 'min' | 'close' | None."""
    if not rects:
        return None
    for key, (rx, ry, rw, rh) in rects.items():
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return key
    return None
