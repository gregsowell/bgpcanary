"""A small PDF writer, for the evidence report an event page downloads (evidence_pdf.py).

It writes text in the standard PDF fonts (Helvetica and Courier: every PDF viewer has them, so nothing is embedded
and the text can be searched and copied), wraps paragraphs, lays out tables that carry on across pages with their
header repeated, preformatted blocks for router output, PNG pictures, shapes and links, and numbers the pages. It
needs nothing beyond Pillow, which bgpcanary already uses for the alert pictures.

The standard fonts draw Windows-1252 (WinAnsiEncoding) only: other characters are written as their nearest ASCII
("é" is drawn, "→" becomes "->", "ō" becomes "o", a character with no such form becomes "?").

Layout runs top to bottom: `y` is measured from the top of the page, and each call places its content at `y` and
moves `y` past it, starting a new page when it doesn't fit."""
import io
import time
import unicodedata
import zlib

from PIL import Image

PAGE = (612.0, 792.0)   # US Letter, in points (1/72 inch)
MARGIN = 42.0
FOOTER = 20.0           # kept free at the bottom of each page for the footer line
REGULAR, BOLD, MONO, MONO_BOLD, ITALIC = "F1", "F2", "F3", "F4", "F5"
FONTS = {REGULAR: "Helvetica", BOLD: "Helvetica-Bold", MONO: "Courier", MONO_BOLD: "Courier-Bold",
         ITALIC: "Helvetica-Oblique"}
COLORS = {"text": (27, 31, 36), "muted": (95, 107, 122), "border": (210, 215, 222), "accent": (36, 99, 235),
          "crit": (200, 38, 43), "warn": (199, 119, 0), "ok": (26, 138, 74), "band": (243, 245, 248),
          "crit_bg": (252, 234, 234), "warn_bg": (253, 243, 226), "info_bg": (233, 240, 253), "white": (255, 255, 255)}
LEAD = 1.3              # line height, as a multiple of the font size

# Glyph widths (1/1000 of the font size) of WinAnsi codes 32 to 126, from Adobe's metrics for the standard fonts
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667,
    778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556,
    556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667,
    778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611,
    611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
# ...and of the codes above 127 that aren't accented letters (those take their base letter's width)
_HIGH = {0x80: 556, 0x82: 222, 0x84: 333, 0x85: 1000, 0x86: 556, 0x87: 556, 0x89: 1000, 0x8B: 333, 0x91: 222,
         0x92: 222, 0x93: 333, 0x94: 333, 0x95: 350, 0x96: 556, 0x97: 1000, 0x99: 1000, 0x9B: 333, 0xA0: 278,
         0xA9: 737, 0xAB: 556, 0xAE: 737, 0xB0: 400, 0xB1: 584, 0xB7: 278, 0xBB: 556, 0xD7: 584, 0xF7: 584}
# characters bgpcanary's texts use that Windows-1252 lacks
_ASCII = {"→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "✓": "ok", "✔": "ok", "✗": "x", "✕": "x", "≥": ">=",
          "≤": "<=", "≠": "!=", "≈": "~", "−": "-", "‐": "-", "‑": "-", "▶": ">", "▼": "v", "▲": "^", "❚": "|",
          "✂": "x", " ": " ", " ": " ", "​": ""}


def _widths(table):
    w = [0] * 32 + table + [556]
    for code in range(128, 256):
        if code in _HIGH:
            w.append(_HIGH[code])
            continue
        base = unicodedata.normalize("NFKD", bytes([code]).decode("cp1252", "replace"))[:1]
        w.append(table[ord(base) - 32] if " " <= base <= "~" else 556)
    return w


_W = {REGULAR: _widths(_HELV), BOLD: _widths(_HELV_BOLD), ITALIC: _widths(_HELV)}


def encode(text):
    """`text` as WinAnsiEncoding bytes, what the standard fonts can't draw replaced (see the module's notes)."""
    s = str(text)
    if s.isascii() and s.isprintable():   # (most of it: AS paths, addresses, router output)
        return s.encode("ascii")
    out = bytearray()
    for ch in s:
        if ch < " ":
            out += b" "
            continue
        try:
            out += ch.encode("cp1252")
            continue
        except UnicodeEncodeError:
            pass
        sub = _ASCII.get(ch)
        if sub is None:
            sub = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode() or "?"
        out += sub.encode("cp1252", "replace")
    return bytes(out)


def width(text, font=REGULAR, size=10.0):
    """How wide `text` is drawn, in points."""
    b = encode(text)
    if font in (MONO, MONO_BOLD):
        return len(b) * 0.6 * size
    w = _W[font]
    return sum(w[c] for c in b) * size / 1000


def _lit(b):
    """Bytes as a PDF literal string."""
    out = bytearray(b"(")
    for c in b:
        if c in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([c])
        elif c < 32 or c > 126:
            out += b"\\%03o" % c
        else:
            out.append(c)
    return bytes(out + b")")


def _hex16(s):
    """A string in UTF-16 (the document's title and the like may hold any character)."""
    return b"<FEFF" + str(s).encode("utf-16-be").hex().upper().encode() + b">"


def _n(v):
    return (b"%.2f" % v).rstrip(b"0").rstrip(b".") or b"0"


def _rgb(c):
    c = COLORS.get(c, c) if isinstance(c, str) else c
    return b"%s %s %s" % tuple(_n(v / 255) for v in c)


def _idat(png):
    """The image data of a PNG (its IDAT chunks): a PDF reads it as is, with the PNG predictors."""
    pos, out = 8, []
    while pos + 8 <= len(png):
        n, kind = int.from_bytes(png[pos:pos + 4], "big"), png[pos + 4:pos + 8]
        if kind == b"IDAT":
            out.append(png[pos + 8:pos + 8 + n])
        pos += 12 + n
    return b"".join(out)


class Doc:
    def __init__(self, title="", author="", subject="", creator="", footer=""):
        self.W, self.H = PAGE
        self.x0, self.cw = MARGIN, PAGE[0] - 2 * MARGIN   # content: left edge and width
        self.bottom = PAGE[1] - MARGIN - FOOTER           # content never goes lower than this
        self.info = {"Title": title, "Author": author, "Subject": subject, "Creator": creator}
        self.footer = footer
        self.pages, self.images = [], []
        self.outline = []   # the headings, as the viewer's bookmarks: (level, text, page index, y)
        self.new_page()

    # ------------------------------------------------------------------ drawing, at a place
    def new_page(self):
        self.pages.append({"ops": [], "images": set(), "links": []})
        self.y = MARGIN

    def _op(self, b):
        self.pages[-1]["ops"].append(b)

    def text_at(self, x, y, text, font=REGULAR, size=9.0, color="text"):
        """`text` with its baseline at `y`."""
        self._op(b"BT %s rg /%s %s Tf %s %s Td %s Tj ET" % (_rgb(color), font.encode(), _n(size), _n(x),
                                                             _n(self.H - y), _lit(encode(text))))

    def rect(self, x, y, w, h, fill=None, stroke=None, lw=0.6):
        ops = []
        if fill:
            ops.append(_rgb(fill) + b" rg")
        if stroke:
            ops.append(_rgb(stroke) + b" RG " + _n(lw) + b" w")
        ops.append(b"%s %s %s %s re %s" % (_n(x), _n(self.H - y - h), _n(w), _n(h),
                                           b"B" if fill and stroke else b"f" if fill else b"S"))
        self._op(b" ".join(ops))

    def hline(self, x1, x2, y, color="border", lw=0.6):
        self._op(b"%s RG %s w %s %s m %s %s l S" % (_rgb(color), _n(lw), _n(x1), _n(self.H - y), _n(x2),
                                                    _n(self.H - y)))

    def link(self, x, y, w, h, uri):
        """Makes the area a link to `uri` (top-left corner at x, y)."""
        self.pages[-1]["links"].append((x, self.H - y - h, x + w, self.H - y, uri))

    # ------------------------------------------------------------------ flow: each at `y`, then past it
    def room(self):
        return self.bottom - self.y

    def need(self, h):
        """A new page unless `h` points still fit on this one (or this one is still empty)."""
        if h > self.room() and self.y > MARGIN + 0.5:
            self.new_page()

    def space(self, h):
        self.y = min(self.y + h, self.bottom)

    def wrap(self, text, font=REGULAR, size=9.0, w=None):
        """`text` broken into lines no wider than `w` (the content width): at spaces, and inside a word only when
        the word alone is wider. Its own line breaks are kept."""
        w = self.cw if w is None else w
        lines = []
        for para in str(text).split("\n"):
            cur = ""
            for word in para.split(" "):
                cand = f"{cur} {word}" if cur else word
                if width(cand, font, size) <= w:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                while width(word, font, size) > w and len(word) > 1:
                    lo, hi = 1, len(word) - 1   # the longest start of it that fits (at least one character)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if width(word[:mid], font, size) <= w:
                            lo = mid
                        else:
                            hi = mid - 1
                    lines.append(word[:lo])
                    word = word[lo:]
                cur = word
            lines.append(cur)
        return lines

    def heading(self, text, level=1):
        """A heading (level 0: the document's title, 1: a section, 2: a part of one), kept with what follows."""
        size = {0: 16.0, 1: 13.0, 2: 10.5}[level]
        if self.y > MARGIN + 0.5:
            self.space({0: 0, 1: 12, 2: 8}[level])
        lines = self.wrap(text, BOLD, size)
        self.need(len(lines) * size * LEAD + 40)
        self.outline.append((level, text, len(self.pages) - 1, self.y))
        for ln in lines:
            self.text_at(self.x0, self.y + size, ln, BOLD, size, "accent" if level == 1 else "text")
            self.y += size * LEAD
        if level == 1:
            self.hline(self.x0, self.x0 + self.cw, self.y + 1, "border", 0.8)
            self.y += 4
        self.y += 3

    def para(self, text, size=9.0, font=REGULAR, color="text", indent=0.0, after=3.0, link=None, bullet=False):
        """A wrapped paragraph; `link`: the URI its lines lead to; `bullet`: with a bullet in the indent."""
        x = self.x0 + indent + (9 if bullet else 0)
        lines = self.wrap(text, font, size, self.x0 + self.cw - x)
        for i, ln in enumerate(lines):
            self.need(size * LEAD)
            if bullet and not i:
                self.text_at(x - 8, self.y + size, "•", font, size, color)
            self.text_at(x, self.y + size, ln, font, size, color)
            if link:
                self.link(x, self.y, width(ln, font, size), size * LEAD, link)
            self.y += size * LEAD
        self.y += after

    def kv(self, rows, key_w=118.0, size=9.0):
        """Label and value pairs, the labels in a column of their own: [(label, value)], or with the value's colour
        as a third item. A value can be long: it wraps, and carries on across pages."""
        for row in rows:
            k, v, col = (*row, "text")[:3]
            lines = self.wrap(str(v), REGULAR, size, self.cw - key_w)
            self.need(size * LEAD * min(len(lines), 2))
            self.text_at(self.x0, self.y + size, k, BOLD, size, "muted")
            for ln in lines:
                self.need(size * LEAD)
                self.text_at(self.x0 + key_w, self.y + size, ln, REGULAR, size, col)
                self.y += size * LEAD
            self.y += 1.5
        self.y += 2

    def table(self, cols, rows, size=8.0, pad=2.6):
        """A table that carries on across pages, its header repeated. `cols`: [(title, width, align)], a width in
        points or None for what's left, align "l" or "r". `rows`: lists of cells, each a text or {"text", "font",
        "color"}; or {"cells": [...], "bg": colour} for a shaded row. A cell's text wraps within its column, and
        its own line breaks are kept."""
        fixed = sum(c[1] for c in cols if c[1])
        free = [c for c in cols if not c[1]]
        ws = [c[1] or (self.cw - fixed) / len(free) for c in cols]
        L = size * LEAD

        def head():
            hl = [self.wrap(c[0], BOLD, size - 0.5, w - 2 * pad) for c, w in zip(cols, ws)]
            h = max(len(x) for x in hl) * L + 2 * pad
            self.rect(self.x0, self.y, self.cw, h, fill="band")
            x = self.x0
            for (_t, _w, al), w, lines in zip(cols, ws, hl):
                for i, ln in enumerate(lines):
                    tx = x + w - pad - width(ln, BOLD, size - 0.5) if al == "r" else x + pad
                    self.text_at(tx, self.y + pad + i * L + size, ln, BOLD, size - 0.5, "muted")
                x += w
            self.y += h

        prepared = []
        for row in rows:
            bg, cells = (row.get("bg"), row["cells"]) if isinstance(row, dict) else (None, row)
            cells = [c if isinstance(c, dict) else {"text": "" if c is None else str(c)} for c in cells]
            # (at most 60 lines a cell: a row must fit on one page)
            wrapped = [self.wrap(c["text"], c.get("font", REGULAR), size, w - 2 * pad)[:60] for c, w in zip(cells, ws)]
            prepared.append((bg, cells, wrapped, max(len(x) for x in wrapped) * L + 2 * pad))
        self.need(L * 2 + 4 * pad + (prepared[0][3] if prepared else 0))
        head()
        for bg, cells, wrapped, h in prepared:
            if h > self.room():
                self.new_page()
                head()
            if bg:
                self.rect(self.x0, self.y, self.cw, h, fill=bg)
            x = self.x0
            for c, (_t, _w, al), w, lines in zip(cells, cols, ws, wrapped):
                f = c.get("font", REGULAR)
                for i, ln in enumerate(lines):
                    tx = x + w - pad - width(ln, f, size) if al == "r" else x + pad
                    self.text_at(tx, self.y + pad + i * L + size, ln, f, size, c.get("color", "text"))
                x += w
            self.y += h
            self.hline(self.x0, self.x0 + self.cw, self.y, "border", 0.4)
        self.y += 6

    def pre(self, text, size=7.2, indent=0.0, color="text", bg="band"):
        """Preformatted text (router output, hop lists) in Courier, on a shaded block that carries on across pages.
        A line too long for the page continues on the next line, indented."""
        x = self.x0 + indent
        maxc = max(10, int((self.cw - indent - 8) / (0.6 * size)))
        lines = []
        for raw in str(text).expandtabs(8).split("\n"):
            raw = raw.rstrip()
            while len(raw) > maxc:
                lines.append(raw[:maxc])
                raw = "    " + raw[maxc:]
            lines.append(raw)
        L = size * LEAD
        i = 0
        while i < len(lines):
            self.need(min(len(lines) - i, 3) * L + 6)
            n = max(1, min(len(lines) - i, int((self.room() - 6) // L)))
            if bg:
                self.rect(x, self.y, self.x0 + self.cw - x, n * L + 6, fill=bg)
            for j, ln in enumerate(lines[i:i + n]):
                self.text_at(x + 4, self.y + 3 + j * L + size, ln, MONO, size, color)
            self.y += n * L + 6
            i += n
            if i < len(lines):
                self.new_page()
        self.y += 4

    def image(self, png, w=None, caption=None, max_h=None):
        """A picture (PNG or anything Pillow reads), centred, as wide as `w` (the content width) or as tall as fits
        on a page, with a caption under it."""
        im = Image.open(io.BytesIO(png)).convert("RGB")
        w = min(w or self.cw, self.cw)
        h = w * im.height / im.width
        top = self.bottom - MARGIN - (16 if caption else 4)
        if h > (max_h or top):
            w, h = w * (max_h or top) / h, (max_h or top)
        self.need(h + (16 if caption else 4))
        buf = io.BytesIO()
        im.save(buf, "PNG")   # (RGB, 8 bits, not interlaced: what the image's /DecodeParms below describe)
        self.images.append((im.width, im.height, _idat(buf.getvalue())))
        name = b"Im%d" % len(self.images)
        x = self.x0 + (self.cw - w) / 2
        self._op(b"q %s 0 0 %s %s %s cm /%s Do Q" % (_n(w), _n(h), _n(x), _n(self.H - self.y - h), name))
        self.pages[-1]["images"].add(name)
        self.rect(x, self.y, w, h, stroke="border", lw=0.5)
        self.y += h + 3
        if caption:
            self.para(caption, size=8, font=ITALIC, color="muted", indent=x - self.x0, after=0)
        self.y += 8

    # ------------------------------------------------------------------ the file
    def _footer(self, i, n):
        y = self.H - MARGIN - FOOTER + 9
        ops = [b"%s RG 0.5 w %s %s m %s %s l S" % (_rgb("border"), _n(self.x0), _n(self.H - y), _n(self.x0 + self.cw),
                                                    _n(self.H - y))]
        right = f"Page {i} of {n}"
        for x, t in ((self.x0, self.footer), (self.x0 + self.cw - width(right, REGULAR, 7.5), right)):
            ops.append(b"BT %s rg /F1 7.5 Tf %s %s Td %s Tj ET" % (_rgb("muted"), _n(x), _n(self.H - y - 10),
                                                                    _lit(encode(t))))
        return ops

    def _outlines(self, add, objs, pages):
        """The bookmarks (the headings: sections at the top level, their parts inside them, closed), written as
        objects through `add`; returns the outline's root object. `pages`: the page objects' numbers."""
        tops = []
        for level, text, page, y in self.outline:
            if level <= 1 or not tops:
                tops.append(((text, page, y), []))
            else:
                tops[-1][1].append((text, page, y))
        root = add(b"")
        nums = [(add(b""), [add(b"") for _ in parts]) for _top, parts in tops]

        def item(entry, parent, sibs, i, extra=b""):
            text, page, y = entry
            body = b"<< /Title %s /Parent %d 0 R /Dest [%d 0 R /XYZ 0 %s 0]" % (_hex16(text), parent, pages[page],
                                                                             _n(self.H - y + 4))
            if i:
                body += b" /Prev %d 0 R" % sibs[i - 1]
            if i + 1 < len(sibs):
                body += b" /Next %d 0 R" % sibs[i + 1]
            return body + extra + b" >>"

        top_nums = [t for t, _k in nums]
        for i, ((top, parts), (tn, kn)) in enumerate(zip(tops, nums)):
            extra = b" /First %d 0 R /Last %d 0 R /Count -%d" % (kn[0], kn[-1], len(kn)) if kn else b""
            objs[tn - 1] = item(top, root, top_nums, i, extra)
            for j, (part, cn) in enumerate(zip(parts, kn)):
                objs[cn - 1] = item(part, tn, kn, j)
        objs[root - 1] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (top_nums[0], top_nums[-1],
                                                                                        len(top_nums))
        return root

    def render(self):
        """The PDF file."""
        objs = [b"", b""]   # 1: the catalog, 2: the page tree (both written once the pages are)

        def add(body):
            objs.append(body)
            return len(objs)

        fonts = {k: add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % v.encode())
                 for k, v in FONTS.items()}
        imgs = {}
        for i, (w, h, data) in enumerate(self.images, 1):
            imgs[b"Im%d" % i] = add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                b"/Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 /Columns %d >> "
                b"/Length %d >>\nstream\n" % (w, h, w, len(data)) + data + b"\nendstream")
        font_res = b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in fonts.items())
        kids = []
        for i, pg in enumerate(self.pages, 1):
            body = zlib.compress(b"\n".join(pg["ops"] + self._footer(i, len(self.pages))), 6)
            content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(body) + body + b"\nendstream")
            annots = [add(b"<< /Type /Annot /Subtype /Link /Rect [%s %s %s %s] /Border [0 0 0] /A << /S /URI /URI %s >> >>"
                          % (_n(x1), _n(y1), _n(x2), _n(y2), _lit(uri.encode("ascii", "replace"))))
                      for x1, y1, x2, y2, uri in pg["links"]]
            res = b"<< /Font << %s >>" % font_res
            if pg["images"]:
                res += b" /XObject << %s >>" % b" ".join(b"/%s %d 0 R" % (nm, imgs[nm]) for nm in sorted(pg["images"]))
            res += b" >>"
            page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] /Resources %s /Contents %d 0 R" % (
                _n(self.W), _n(self.H), res, content)
            if annots:
                page += b" /Annots [%s]" % b" ".join(b"%d 0 R" % a for a in annots)
            kids.append(add(page + b" >>"))
        outlines = self._outlines(add, objs, kids) if self.outline else None
        objs[0] = b"<< /Type /Catalog /Pages 2 0 R%s >>" % (
            b" /Outlines %d 0 R /PageMode /UseOutlines" % outlines if outlines else b"")
        objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
        info = add(b"<< " + b" ".join(b"/%s %s" % (k.encode(), _hex16(v)) for k, v in self.info.items() if v)
                   + b" /Producer (bgpcanary) /CreationDate (D:%s) >>" % time.strftime("%Y%m%d%H%M%SZ", time.gmtime()).encode())
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, info, xref)
        return bytes(out)
