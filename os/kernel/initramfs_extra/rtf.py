# rtf.py -- minimal RTF (Rich Text Format) text reader/writer for Ripos (M9.4).
#
# The OS has no Tcl/Tk and no RTF library, so this is a small pure-Python
# RTF codec: it extracts plain text (paragraphs, tabs, escapes, unicode
# \uN, \'hh hex, \* groups, fonts tables are skipped) and writes a
# minimal \rtf1 document from plain text.  Bold/colour/size control words
# are parsed and dropped (the editor is a text editor); the structure is
# kept so the reader is a real RTF parser, not a regex hack.

class RtfError(Exception):
    pass


def _append(out, ch):
    if ch == '\\':
        return
    out.append(ch)


_ANSI_CP = {
    437: 'cp437', 850: 'cp850', 852: 'cp852', 866: 'cp866',
    932: 'cp932', 936: 'cp936', 949: 'cp949', 950: 'cp950',
    54936: 'gb18030', 65001: 'utf-8',
}


def _hex_decode(b, ansi_cp):
    """Decode one \'hh escape with the document's ANSI codepage (e.g.
    \ansicpg54936 -> gb18030), falling back to latin-1 for codepages the
    embedded interpreter does not ship."""
    name = None
    if ansi_cp:
        name = _ANSI_CP.get(ansi_cp)
        if name is None and 1250 <= ansi_cp <= 1258:
            name = 'cp%d' % ansi_cp
    if name is None:
        try:
            b = b.decode("latin-1")
        except UnicodeDecodeError:
            b = '?'
        return b
    try:
        import codecs
        return codecs.lookup(name).decode(b, "replace")[0]
    except (LookupError, ImportError):
        return b.decode("latin-1", "replace")


def load_rtf(data):
    """Extract plain text from RTF bytes."""
    if isinstance(data, str):
        data = data.encode("latin-1")
    pos = 0
    n = len(data)
    out = []
    in_group = False
    in_font_table = False
    font_table_depth = 0
    skip = False          # inside a \* destination
    unicode_skip = 0      # remaining \ucN fallback chars to drop
    ansi_cp = 0           # \ansicpg parameter
    pend = bytearray()    # raw multibyte bytes (\'hh escapes or raw bytes)
                          # awaiting a codepage decode once the run ends
    def flush_pend():
        if pend:
            out.append(_hex_decode(bytes(pend), ansi_cp))
            del pend[:]
    while pos < n:
        b = data[pos]
        pos += 1
        if b == 0x5C:  # backslash
            if pos >= n:
                break
            c = data[pos]
            if c == 0x27:  # \'hh hex
                if pos + 2 < n:
                    try:
                        raw = bytes([int(data[pos + 1:pos + 3], 16)])
                    except ValueError:
                        raw = None
                    if raw is not None:
                        if unicode_skip > 0:
                            unicode_skip -= 1   # redundant \uN fallback byte
                        elif not (in_font_table or skip):
                            # buffer: CJK chars are \'hh\'hh sequences, and
                            # they must be decoded together as one run
                            pend.append(raw[0])
                    pos += 3
                continue
            # any other backslash ends a raw/hex run
            if pend:
                flush_pend()
            if c == 0x5C:  # \\
                out.append('\\')
                pos += 1
                continue
            if c == 0x7B:  # \{
                out.append('{')
                pos += 1
                continue
            if c == 0x7D:  # \}
                out.append('}')
                pos += 1
                continue
            if c == 0x2A:  # \* skip destination
                skip = True
                pos += 1
                continue
            if c == 0x0D or c == 0x0A:  # \\r \\n raw newline
                pos += 1
                continue
            # control word: [a-z]+ [optional param] [optional space]
            j = pos
            while j < n and 97 <= data[j] <= 122:
                j += 1
            word = data[pos:j].decode("ascii")
            param = 0
            sign = 1
            k = j
            if k < n and data[k] == 0x2D:
                sign = -1
                k += 1
            pstart = k
            while k < n and 48 <= data[k] <= 57:
                k += 1
            if k > pstart:
                param = sign * int(data[pstart:k])
            # consume the delimiter space (required by the spec)
            if k < n and data[k] == 0x20:
                k += 1
            pos = k
            if word == 'par' or word == 'line':
                out.append('\n')
            elif word == 'tab':
                out.append('\t')
            elif word == 'u':
                # \uN: the unicode char, then \ucN fallback bytes to skip
                try:
                    out.append(chr(param & 0xFFFF))
                except ValueError:
                    pass
                unicode_skip = 1
                continue
            elif word == 'uc':
                continue
            elif word == 'fonttbl':
                in_font_table = True
                continue
            elif word == 'colortbl' or word == 'stylesheet' or word == 'info':
                skip = True
                continue
            elif word == 'rtf' or word == 'ansi' or word == 'deff':
                continue
            elif word == 'ansicpg':
                if param:
                    ansi_cp = param
                continue
            # any other control word: nothing to emit (styles dropped)
            continue
        elif b == 0x7B:  # {
            if in_font_table:
                font_table_depth += 1
            continue
        elif b == 0x7D:  # }
            if in_font_table:
                font_table_depth -= 1
                if font_table_depth <= 0:
                    in_font_table = False
            skip = False
            continue
        elif b == 0x0D or b == 0x0A:
            continue
        else:
            if in_font_table or skip:
                continue
            if unicode_skip > 0:
                unicode_skip -= 1
                continue
            if b >= 0x80:
                # raw multibyte run: buffer until the next ASCII separator
                pend.append(b)
                continue
            if pend:
                flush_pend()
            if b:
                try:
                    ch = bytes([b]).decode("latin-1")
                except UnicodeDecodeError:
                    ch = '?'
                out.append(ch)
    if pend:
        flush_pend()
    text = ''.join(out)
    # normalize paragraph breaks and drop any stray trailing NUL (some
    # editors append one after the closing brace)
    return text.rstrip('\x00')


def save_rtf(text):
    """Write plain text as a minimal RTF document (\rtf1 + escaped text)."""
    parts = ["{\\rtf1\\ansi", "{\\fonttbl{\\f0 Courier;}}", "{\\f0 "]
    for i, line in enumerate(text.split('\n')):
        if i:
            parts.append("\\par ")
        esc = []
        for ch in line:
            o = ord(ch)
            if ch == '\\':
                esc.append('\\\\')
            elif ch == '{':
                esc.append('\\{')
            elif ch == '}':
                esc.append('\\}')
            elif o == 0x09:
                esc.append('\\tab ')
            elif 32 <= o < 127:
                esc.append(ch)
            else:
                esc.append('\\u%d?' % o)
        parts.append(''.join(esc))
    parts.append('}')
    return ''.join(parts).encode("ascii", "replace")
