"""Reads back what bgpcanary.pdf writes, as a PDF viewer would start to: the cross-reference table must point at each
object exactly, and the page contents (Flate-compressed) give back the text drawn. Shared by the PDF tests."""
import re
import zlib


def objects(data):
    """{number: body} of every object, checked against the cross-reference table (each offset must land on
    "N 0 obj"), and the trailer."""
    assert data.startswith(b"%PDF-1.4\n") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF\s*$", data)[1])
    assert data[start:start + 5] == b"xref\n"
    m = re.match(rb"xref\n0 (\d+)\n", data[start:])
    n = int(m[1])
    rows = data[start + m.end():start + m.end() + 20 * n]
    assert rows[:20] == b"0000000000 65535 f \n"
    out = {}
    for i in range(1, n):
        row = rows[20 * i:20 * i + 20]
        assert re.fullmatch(rb"\d{10} 00000 n \n", row), row
        off = int(row[:10])
        head = b"%d 0 obj\n" % i
        assert data[off:off + len(head)] == head, (i, data[off:off + 20])
        end = data.index(b"\nendobj\n", off)
        out[i] = data[off + len(head):end]
    trailer = data[start + m.end() + 20 * n:]
    assert re.search(rb"/Size %d /Root 1 0 R" % n, trailer)
    return out


def stream(body):
    """An object's stream data, inflated when it's Flate-compressed (images keep their PNG predictors)."""
    m = re.match(rb"<<(.*?)>>\nstream\n", body, re.S)
    raw = body[m.end():]
    assert raw.endswith(b"\nendstream")
    raw = raw[:-len(b"\nendstream")]
    n = int(re.search(rb"/Length (\d+)", m[1])[1])
    assert len(raw) == n, (len(raw), n)
    return zlib.decompress(raw) if b"/FlateDecode" in m[1] else raw


def _unlit(s):
    """A PDF literal string's bytes (without its parentheses), escapes undone, as Windows-1252 text."""
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == 0x5C:
            nxt = s[i + 1:i + 4]
            if nxt[:1].isdigit():
                out.append(int(nxt, 8))
                i += 4
                continue
            out.append(s[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("cp1252")


def pages(data):
    """[[text drawn on the page, in order]] for each page, in page order."""
    objs = objects(data)
    kids = [int(k) for k in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[(.*?)\]", objs[2])[1])]
    out = []
    for k in kids:
        c = int(re.search(rb"/Contents (\d+) 0 R", objs[k])[1])
        ops = stream(objs[c])
        out.append([_unlit(m) for m in re.findall(rb"\(((?:[^\\()]|\\.)*)\) Tj", ops, re.S)])
    return out


def text(data):
    return "\n".join(t for p in pages(data) for t in p)


def outline(data):
    """The bookmarks: [(title, page number, [(title, page number)...])], in order."""
    objs = objects(data)
    m = re.search(rb"/Outlines (\d+) 0 R", objs[1])
    if not m:
        return []
    kids = [int(k) for k in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[(.*?)\]", objs[2])[1])]

    def entry(n):
        body = objs[n]
        title = bytes.fromhex(re.search(rb"/Title <FEFF([0-9A-F]*)>", body)[1].decode()).decode("utf-16-be")
        page = kids.index(int(re.search(rb"/Dest \[(\d+) 0 R", body)[1])) + 1
        return title, page, body

    def chain(first):
        out, n = [], first
        while n:
            title, page, body = entry(n)
            f = re.search(rb"/First (\d+) 0 R", body)
            out.append((title, page, [(t, p) for t, p, _k in chain(int(f[1]))] if f else []))
            nx = re.search(rb"/Next (\d+) 0 R", body)
            n = int(nx[1]) if nx else None
        return out
    return chain(int(re.search(rb"/First (\d+) 0 R", objs[int(m[1])])[1]))
