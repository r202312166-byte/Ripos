# i18n/zh.py -- Chinese UI strings for the Ripos window manager (UTF-8).
STRINGS = {
    # desktop / chrome
    "os.title": "Ripos",
    "os.subtitle": "Rust 解释型 Python 操作系统",
    "desktop.hint": "Tab: 切换焦点  方向键: 移动窗口  Ctrl+L: 切换语言  输入: 终端",

    # windows
    "win.system": "系统信息",
    "win.terminal": "终端",
    "win.language": "语言",
    "win.widgets": "组件 (tk)",

    # system info window content
    "sys.kernel": "内核: Ripos (no_std Rust)",
    "sys.python": "Python: {py} (LP64, 独立运行)",
    "sys.modules": "内置模块数: {n}",
    "sys.uptime": "运行时间: {s} 秒",
    "sys.heap": "C 堆: {used}/{total}",
    "sys.fb": "帧缓冲: {w}x{h} fmt={fmt}",

    # terminal window content
    "term.prompt": ">",
    "term.status": "输入, 回车执行, 退格删除",

    # language window content
    "lang.label": "当前语言:",
    "lang.en": "英语",
    "lang.zh": "中文",
    "lang.hint": "Ctrl+L 切换 / Ctrl+L switches",

    # app window system (appbar sidebar)
    "appbar.apps": "应用", "appbar.running": "正在运行",

    # file manager
    "fm.up": "向上", "fm.back": "后退", "fm.fwd": "前进", "fm.home": "主页",
    "fm.new": "新建", "fm.del": "删除", "fm.run": "运行", "fm.open": "打开",
    "fm.editor": "编辑器", "fm.quit": "退出",
    "fm.open_with": "打开方式", "fm.pinned": "固定文件夹",
    "fm.items": "{n} 项 | {path}", "fm.cannot_read": "无法读取 {path}",
    "fm.error": "(错误: {e})", "fm.empty": "(空)",
    "fm.new_folder": "新建文件夹", "fm.new_folder_n": "新建文件夹 {n}",
    "fm.created": "已创建 {p}", "fm.home_only": "只有 /home 可写; 已在其中创建 {p}",
    "fm.mkdir_fail": "创建目录 {p}: {e}", "fm.read_only": "只读: 只能删除 /home 中的条目",
    "fm.deleted": "已删除 {p}", "fm.delete_fail": "删除 {p}: {e}",
    "fm.select_py": "请选择一个 .py 文件来运行", "fm.not_file": "{p} 不是文件",
    "fm.run_py_only": "运行仅支持 .py 文件 ({p})", "fm.ran_no_out": "已运行 {p} (无输出)",
    "fm.open_fail": "打开 {p}: {e}", "fm.view_fail": "查看 {p}: {e}",
    "fm.archive_fail": "归档 {p}: {e}", "fm.unknown_app": "未知应用: {a}",
    "fm.dbl_open": "{p} ({s}) -- 点击 打开 选择应用",

    # editor
    "ed.open": "打开", "ed.run_f5": "运行 F5", "ed.save_f2": "保存 F2",
    "ed.new": "新建", "ed.quit": "退出", "ed.prompt": ">>> ",
    "ed.hint": "Ripos 编辑器 -- F5 运行缓冲, F6 切换窗格",
    "ed.opened": "已打开 {p} ({n} 行)", "ed.saved": "已保存 {p} ({n} 字节)",
    "ed.new_buffer": "新缓冲 (F2 保存)",
    "ed.no_path": "无路径: 在工具栏输入路径后按 F2",
    "ed.undo": "撤销", "ed.copied": "已复制 {n} 字符到剪贴板",
    "ed.open_fail": "打开 {p}: {e}", "ed.save_fail": "保存 {p}: {e}",
}
