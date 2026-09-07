# mirror-toggle-test.py -- host check that toggling Framebuffer.mirror_x
# flips where writes land (stubs kern; no OS boot).
import sys, pathlib

class _Kern:
    def __init__(self):
        self.w = 320; self.h = 240; self.bpp = 3; self.stride = self.w
        self.mem = memoryview(bytearray(self.w * self.h * self.bpp))
    def fb_info(self): return (self.w, self.h, self.stride, self.bpp, 0)
    def fb_mem(self): return self.mem
    def write(self, s): pass
    def after(self, ms, cb): pass
    def tick(self): return 0
    def on_key(self, cb): pass

kern = _Kern()
sys.modules['kern'] = kern
_extra = pathlib.Path(__file__).resolve().parents[1] / 'kernel' / 'initramfs_extra'
sys.path.insert(0, str(_extra))
from framebuffer import Framebuffer
import text as textmod

fb = Framebuffer()
assert fb.mirror_x is False, 'default must be no mirror'
assert fb.mirror_glyphs is False, 'default must be no glyph mirror'

def phys(x):
    return x * fb.bpp

# no mirror: logical (0,0) -> physical col 0; (w-1,0) -> physical col w-1
fb.clear(0, 0, 0)
fb.set_pixel(0, 0, 255, 0, 0)
assert fb.mem[phys(0) + 2] == 255 and fb.mem[phys(fb.width - 1) + 2] == 0, 'unmirrored write wrong'
assert fb.pixel(0, 0) == (255, 0, 0)

# toggle on: logical (0,0) -> physical col w-1
fb.mirror_x = True
fb.clear(0, 0, 0)
fb.set_pixel(0, 0, 255, 0, 0)
assert fb.mem[phys(fb.width - 1) + 2] == 255 and fb.mem[phys(0) + 2] == 0, 'mirrored write wrong'
assert fb.pixel(0, 0) == (255, 0, 0), 'mirrored readback disagrees'

# toggle back off
fb.mirror_x = False
fb.clear(0, 0, 0)
fb.set_pixel(0, 0, 255, 0, 0)
assert fb.mem[phys(0) + 2] == 255, 'toggle back off failed'

# ---- MIRROR_GLYPHS: per-glyph flip, positions unchanged ----
WHITE = (255, 255, 255)
BG = (0, 0, 0)

def glyph_patch(x, y, ch):
    """Read back the 16x16 cell at (x, y) as a bit grid."""
    g = []
    for row in range(16):
        bits = 0
        for c in range(16):
            if fb.pixel(x + c, y + row) == WHITE:
                bits |= 1 << (15 - c)
        g.append(bits)
    return g

def ink_w(patch):
    """The glyph ink width: the readback stores ink in bits
    [16-w .. 15] (the glyph is painted at the left of its cell); w = the
    number of leading (leftmost) columns that carry ink."""
    lows = [b & -b for b in patch if b]
    if not lows:
        return 0
    return 16 - (min(lows).bit_length() - 1)

def rev_w(b, w):
    """Reverse the leftmost w bits of a 16-bit row (the ink window)."""
    out = 0
    for c in range(w):
        if b & (1 << (15 - c)):
            out |= 1 << (15 - (w - 1 - c))
    return out

# draw 'p' normally; with mirror_glyphs on the same glyph must be the
# exact horizontal bit-reversal WITHIN its ink width, and the glyph must
# not be symmetric (a flipped 'p' is not 'p' -- the stem moves side)
fb.clear(*BG)
textmod.draw_char(fb, 10, 10, 'p', WHITE, scale=1)
p_normal = glyph_patch(10, 10, 'p')

fb.mirror_glyphs = True
fb.clear(*BG)
textmod.draw_char(fb, 10, 10, 'p', WHITE, scale=1)
p_flipped = glyph_patch(10, 10, 'p')

w = ink_w(p_normal)
assert w > 0, 'p painted nothing'
assert p_flipped == [rev_w(b, w) for b in p_normal], 'glyph flip must reverse bits'
assert p_normal != p_flipped, 'p must not be symmetric under the flip'
# sanity: the stem side swaps -- within the ink window the lower-left
# ink (the descender stem) is heavier in a normal 'p' than lower-right,
# and vice versa when flipped
def within_w(b, w):
    return b >> (16 - w)
def half_density(patch, lo, hi, w):
    hw = w // 2
    left = right = 0
    for b in patch[lo:hi]:
        v = within_w(b, w)
        left += (v >> (w - hw)) if w > hw else 0
        right += v & ((1 << hw) - 1)
    return left, right
l, r = half_density(p_normal, 9, 15, w)
assert l > r, 'normal p stem should sit left: left=%d right=%d' % (l, r)
l, r = half_density(p_flipped, 9, 15, w)
assert r > l, 'flipped p stem should sit right: left=%d right=%d' % (l, r)

# symmetric glyphs are invariant under the flip; positions unchanged
fb.clear(*BG)
textmod.draw_char(fb, 70, 30, 'I', WHITE, scale=1)
i_normal = glyph_patch(70, 30, 'I')
fb.mirror_glyphs = True
fb.clear(*BG)
textmod.draw_char(fb, 70, 30, 'I', WHITE, scale=1)
assert glyph_patch(70, 30, 'I') == i_normal, 'symmetric glyph changed under flip'

fb.mirror_glyphs = False
fb.clear(*BG)
textmod.draw_char(fb, 10, 10, 'p', WHITE, scale=1)
assert glyph_patch(10, 10, 'p') == p_normal, 'toggle back off failed'

print('MIRROR-TOGGLE-TEST: all assertions passed')
