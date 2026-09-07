# i18n/__init__.py -- language manager for the Ripos window manager.
#
# Pure Python: the kernel just renders pixels and reports keys; the UI
# strings and the switch between English and Chinese live here.

import i18n.en as en
import i18n.zh as zh

LANGUAGES = {
    "en": en.STRINGS,
    "zh": zh.STRINGS,
}

current = "en"


def set_language(code):
    """Switch the active UI language ('en' or 'zh'); returns the code.

    The choice is persisted to /home/settings.json so every surface that
    re-syncs from settings (fm, editor, config, appbar) picks it up --
    the WM's Ctrl+L toggle and the config app both go through here.
    """
    global current
    if code in LANGUAGES:
        current = code
        try:
            import settings
            if settings.get("language") != code:
                settings.set("language", code)
        except Exception:
            pass
    return current


def tr(key, **kwargs):
    """Translate a key to the active language's string, formatting %(kw)s-style."""
    s = LANGUAGES[current].get(key, key)
    if kwargs:
        try:
            return s % kwargs
        except Exception:
            return s
    return s


def trf(key, **kwargs):
    """Translate a key with {name}-style formatting (str.format)."""
    s = LANGUAGES[current].get(key, key)
    if kwargs:
        try:
            return s.format(**kwargs)
        except Exception:
            return s
    return s


def sync_from_settings():
    """Adopt the language chosen in the settings app (/home/settings.json)."""
    try:
        import settings
        set_language(settings.get("language", "en"))
    except Exception:
        pass


def languages():
    return list(LANGUAGES)


def name(code=None):
    c = code or current
    if c == "zh":
        return "中文"
    return "English"
