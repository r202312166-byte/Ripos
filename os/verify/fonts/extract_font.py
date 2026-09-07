
import re
import os

# --- ASCII 8x8 from font8x8_basic.h (public domain) ---
ascii_glyphs = {}
with open(r'target\font8x8_basic.h', encoding='ascii') as f:
    content = f.read()
# glyphs are: const char font8x8_basic[128][8] = { {0x00,...}, ... }
body = content[content.index('{'):]
# find each { ... } block of 8 bytes
for m in re.finditer(r'\{\s*((?:0x[0-9A-Fa-f]{2}\s*,\s*){7}0x[0-9A-Fa-f]{2})\s*\}', body):
    vals = [int(x, 16) for x in re.findall(r'0x([0-9A-Fa-f]{2})', m.group(1))]
    if len(vals) != 8:
        continue
    idx = len(ascii_glyphs)
    ascii_glyphs[idx] = bytes(vals)
    if idx >= 0x7F:
        break

# --- CJK 16x16 from Unifont hex ---
needed = set()
for path in [r'kernel\initramfs_extra\i18n\zh.py',
             r'kernel\initramfs_extra\i18n\__init__.py']:
    s = open(path, encoding='utf-8').read()
    for ch in s:
        if ord(ch) > 0x7F:
            needed.add(ord(ch))

cjk_glyphs = {}
with open(r'target\unifont.hex', encoding='ascii') as f:
    for line in f:
        m = re.match(r'^([0-9A-Fa-f]{4,6}):([0-9A-Fa-f]+)$', line.strip())
        if not m:
            continue
        cp = int(m.group(1), 16)
        if cp in needed:
            cjk_glyphs[cp] = bytes.fromhex(m.group(2))

# --- write fonts/unicode.py ---
os.makedirs(r'kernel\initramfs_extra\fonts', exist_ok=True)
out = []
out.append('"""fonts/unicode.py -- bitmap glyphs for the Ripos GUI.')
out.append('ASCII: 8x8 (8 bytes) from font8x8_basic.h by Daniel Hepper (public domain).')
out.append('CJK:   16x16 (32 bytes) from GNU Unifont 15.1.05 (GPLv2+ with the font')
out.append('       embedding exception).  Codepoints < 0x80 are 8x8; >= 0x80 are 16x16."""')
out.append('')
out.append('GLYPHS = {')
for cp in sorted(ascii_glyphs):
    out.append('    %#x: %r,' % (cp, ascii_glyphs[cp]))
for cp in sorted(cjk_glyphs):
    out.append('    %#x: %r,' % (cp, cjk_glyphs[cp]))
out.append('}')
open(r'kernel\initramfs_extra\fonts\unicode.py', 'w', encoding='ascii').write('\n'.join(out))
missing = [hex(cp) for cp in sorted(needed) if cp not in cjk_glyphs]
print('ascii:', len(ascii_glyphs), 'cjk:', len(cjk_glyphs), 'missing:', missing)
