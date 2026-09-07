"""tkinter.messagebox -- serial-only dialog stubs for the Ripos tkinter module.

Ripos is keyboard-only with a single screen, so dialogs cannot be shown.
The showinfo/askyesno family are stubs that log the dialog to the serial
console and return the conventional default answer ('ok', True, ...), so
programs that use messagebox for confirmation keep running.  Interactive
dialogs are out of scope.
"""

import kern


def _dialog(kind, title, message, **kw):
    kern.write('tkinter.messagebox.%s: %s: %s\n' % (kind, title, message))


def showinfo(title=None, message=None, **kw):
    _dialog('showinfo', title, message)
    return 'ok'


def showwarning(title=None, message=None, **kw):
    _dialog('showwarning', title, message)
    return 'ok'


def showerror(title=None, message=None, **kw):
    _dialog('showerror', title, message)
    return 'ok'


def askyesno(title=None, message=None, **kw):
    _dialog('askyesno', title, message)
    return True


def askokcancel(title=None, message=None, **kw):
    _dialog('askokcancel', title, message)
    return True


def askretrycancel(title=None, message=None, **kw):
    _dialog('askretrycancel', title, message)
    return True


def askquestion(title=None, message=None, **kw):
    _dialog('askquestion', title, message)
    return 'yes'


def askyesnocancel(title=None, message=None, **kw):
    _dialog('askyesnocancel', title, message)
    return True
