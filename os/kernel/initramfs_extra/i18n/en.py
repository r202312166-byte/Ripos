# i18n/en.py -- English UI strings for the Ripos window manager.
STRINGS = {
    # desktop / chrome
    "os.title": "Ripos",
    "os.subtitle": "Rust interpreted Python Operating System",
    "desktop.hint": "Tab: focus  Arrows: move  Ctrl+L: lang  Type",

    # windows
    "win.system": "System Info",
    "win.terminal": "Terminal",
    "win.language": "Language",
    "win.widgets": "Widgets (tk)",

    # system info window content
    "sys.kernel": "kernel: Ripos (no_std Rust)",
    "sys.python": "python: {py} (LP64, freestanding)",
    "sys.modules": "builtin modules: {n}",
    "sys.uptime": "uptime: {s}s",
    "sys.heap": "c-heap: {used}/{total}",
    "sys.fb": "framebuffer: {w}x{h} fmt={fmt}",

    # terminal window content
    "term.prompt": ">",
    "term.status": "type, Enter to run, Backspace to erase",

    # language window content
    "lang.label": "Current language:",
    "lang.en": "English",
    "lang.zh": "Chinese",
    "lang.hint": "Ctrl+L switches / Ctrl+L 切换语言",

    # app window system (appbar sidebar)
    "appbar.apps": "APPS", "appbar.running": "RUNNING",

    # file manager
    "fm.up": "Up", "fm.back": "Back", "fm.fwd": "Fwd", "fm.home": "Home",
    "fm.new": "New", "fm.del": "Del", "fm.run": "Run", "fm.open": "Open",
    "fm.editor": "Editor", "fm.quit": "Quit",
    "fm.open_with": "Open with", "fm.pinned": "Pinned folders",
    "fm.items": "{n} items | {path}", "fm.cannot_read": "cannot read {path}",
    "fm.error": "(error: {e})", "fm.empty": "(empty)",
    "fm.new_folder": "New Folder", "fm.new_folder_n": "New Folder {n}",
    "fm.created": "created {p}", "fm.home_only": "only /home is writable; created {p} there",
    "fm.mkdir_fail": "mkdir {p}: {e}", "fm.read_only": "read-only: only /home entries can be deleted",
    "fm.deleted": "deleted {p}", "fm.delete_fail": "delete {p}: {e}",
    "fm.select_py": "select a .py file to run", "fm.not_file": "{p} is not a file",
    "fm.run_py_only": "Run works on .py files ({p})", "fm.ran_no_out": "ran {p} (no output)",
    "fm.open_fail": "open {p}: {e}", "fm.view_fail": "view {p}: {e}",
    "fm.archive_fail": "archive {p}: {e}", "fm.unknown_app": "unknown app: {a}",
    "fm.dbl_open": "{p} ({s}) -- Open chooses an app",

    # editor
    "ed.open": "Open", "ed.run_f5": "Run F5", "ed.save_f2": "Save F2",
    "ed.new": "New", "ed.quit": "Quit", "ed.prompt": ">>> ",
    "ed.hint": "Ripos editor -- F5 runs the buffer, F6 switches panes",
    "ed.opened": "opened {p} ({n} lines)", "ed.saved": "saved {p} ({n} bytes)",
    "ed.new_buffer": "new buffer (F2 to save)",
    "ed.no_path": "no path: type a path in the bar, then F2",
    "ed.undo": "undo", "ed.copied": "copied {n} chars to clipboard",
    "ed.open_fail": "open {p}: {e}", "ed.save_fail": "save {p}: {e}",
}
