# docx.py -- minimal DOCX (Office Open XML) text reader/writer for Ripos (M9.4).
#
# A .docx is a ZIP container holding word/document.xml (plus a couple of
# fixed part files).  This module reads the paragraphs/runs of the body
# with xml.etree.ElementTree and writes a minimal but valid document:
#   load_docx(data) -> str     (plain text, paragraphs joined by \n)
#   save_docx(text) -> bytes
# Requires the stdlib zipfile + xml.etree (M9.1 zlib makes ZIP_DEFLATED
# work; xml.etree works via the pure-Python fallback or _elementtree).

import os
import xml.etree.ElementTree as ET
import zipfile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS


def load_docx(data):
    """Extract plain text from a DOCX file (bytes)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    z = zipfile.ZipFile(__import__("io").BytesIO(data))
    xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    if body is None:
        body = root
    lines = []
    for p in body.iter(W + "p"):
        parts = []
        for node in p.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag == W + "br" or node.tag == W + "cr":
                parts.append("\n")
        # join runs; drop the paragraph's own text (handled as runs)
        text = "".join(parts)
        if text or True:
            lines.append(text)
    return "\n".join(lines)


def save_docx(text):
    """Write plain text as a minimal DOCX (single section, Courier-ish)."""
    if isinstance(text, str):
        text = text.encode("utf-8")
    text = text.decode("utf-8")
    paragraphs = text.split("\n")
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="%s"><w:body>' % W_NS,
    ]
    for para in paragraphs:
        xml_parts.append("<w:p><w:r><w:t xml:space='preserve'>%s</w:t></w:r></w:p>"
                         % _xml_escape(para))
    xml_parts.append("</w:body></w:document>")
    document = "".join(xml_parts)

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))
