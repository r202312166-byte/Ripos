# layout-test.py -- verify the tk pack fix: toolbar top, status bottom, editor middle.
import sys, pathlib
class _Kern:
    def __init__(self):
        self.w, self.h, self.bpp = 800, 600, 3
        self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
        self.log = []
    def fb_info(self): return (self.w, self.h, self.stride, self.bpp, 0)
    def fb_mem(self): return self.mem
    def write(self, s): self.log.append(s)
    def after(self, ms, cb): pass
    def tick(self): return 0
    def on_key(self, cb): pass
kern = _Kern()
sys.modules['kern'] = kern
_extra = pathlib.Path(__file__).resolve().parents[1] / 'kernel' / 'initramfs_extra'
sys.path.insert(0, str(_extra))
import tk
from framebuffer import Framebuffer

class FakeKB:
    def __init__(self): self.handlers = []
    def on_event(self, h): self.handlers.append(h)
    def ctrl_down(self): return False

fb = Framebuffer()

# --- fm layout: bar(top), path(top), listbox(expand both), status(bottom)
r = tk.Tk(title='fm')
r.start(fb, FakeKB())
bar = tk.Frame(r, bg=(30, 36, 48))
for txt in ('Up', 'Back', 'Fwd', 'Home', 'Editor', 'Quit'):
    tk.Button(bar, text=txt).pack(side='left', padx=2, pady=2)
bar.pack(side='top', fill='x')
pl = tk.Label(r, text='/home', fg=(200, 210, 230), bg=(26, 32, 42), padx=6, pady=2)
pl.pack(side='top', fill='x')
lb = tk.Listbox(r, height=24)
lb.pack(fill='both', expand=True, padx=4, pady=4)
st = tk.Label(r, text='status', fg=(150, 165, 180), bg=(22, 26, 34), padx=6, pady=2)
st.pack(side='bottom', fill='x')

def p(name, w):
    print('%s: x=%d y=%d w=%d h=%d' % (name, w.x, w.y, w.w, w.h))

p('bar', bar); p('path', pl); p('listbox', lb); p('status', st)
assert bar.y <= 30 and bar.h > 0, 'toolbar must be at the top (got y=%d)' % bar.y
assert st.y + st.h >= 590, 'status must sit at the bottom (got %d+%d)' % (st.y, st.h)
assert lb.y >= pl.y + pl.h, 'listbox below path bar'
assert lb.y + lb.h <= st.y, 'listbox must NOT overlap the status bar'
assert lb.h > 300, 'listbox must be the big middle area (got %d)' % lb.h
print('FM LAYOUT OK')

# --- editor layout: status(bottom), pane(bottom), editor(expand), bar(top)
r2 = tk.Tk(title='editor')
r2.start(fb, FakeKB())
ed = tk.Widget(r2)
ed._natw, ed._nath = 400, 120
st2 = tk.Label(r2, text='status', fg=(150, 165, 180), bg=(22, 26, 34), padx=6, pady=2)
st2.pack(side='bottom', fill='x')
pane = tk.Label(r2, text='pane', bg=(30, 36, 48))
pane._nath = 144
pane.pack(side='bottom', fill='x')
ed.pack(fill='both', expand=True)
bar2 = tk.Frame(r2, bg=(30, 36, 48))
tk.Button(bar2, text='Open').pack(side='left', padx=2, pady=2)
bar2.pack(side='top', fill='x')

p('ed_bar', bar2); p('ed', ed); p('pane', pane); p('ed_status', st2)
assert bar2.y <= 30, 'editor toolbar must be at the top (got y=%d)' % bar2.y
assert st2.y + st2.h >= 590, 'status at bottom'
assert ed.y >= bar2.y + bar2.h, 'editor below toolbar'
assert ed.y + ed.h <= pane.y, 'editor above pane, no overlap'
assert pane.y + pane.h <= st2.y, 'pane above status'
print('EDITOR LAYOUT OK')
print('ALL LAYOUT TESTS PASSED')
