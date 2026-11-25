"""bgpcanary's small PDF writer (bgpcanary/pdf.py), read back as a viewer would start to (tests/pdfcheck.py): the
cross-reference table points at every object exactly, the pages' text comes back, text the standard fonts can't draw
is replaced, and tables, preformatted blocks and pictures carry on across pages."""
import io
import re

from PIL import Image

import pdfcheck


def png(w=400, h=200):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (36, 99, 235)).save(buf, "PNG")
    return buf.getvalue()


def test_a_document_reads_back():
    from bgpcanary import pdf
    d = pdf.Doc(title="Event 7: an outage → seen", author="AS64500", footer="AS64500 · event 7")
    d.heading("192.0.2.0/24 outage (as seen by RIS) \\ and more", 0)
    d.kv([("Started", "2026-09-29 18:54:12 UTC"), ("Severity", "CRITICAL", "crit")])
    d.heading("Around this event", 1)
    d.para("Routes moved → elsewhere: café ✓ 日本", bullet=True)
    d.para("https://atlas.ripe.net/measurements/900000001/", link="https://atlas.ripe.net/measurements/900000001/")
    d.heading("Part of it", 2)
    d.image(png(), caption="Before the event (2026-09-29 18:50 UTC)")
    d.table([("Time", 90, "l"), ("What happened", None, "l"), ("Peers", 50, "r")],
            [["18:54:12", "BGP: 27 of 40 RIS peers changed path", "27"], {"cells": ["18:55", "loss", "9"], "bg": "crit_bg"}])
    d.pre("*> 192.0.2.0/24  198.51.100.1  0 64496 64500 i")
    data = d.render()

    objs = pdfcheck.objects(data)   # (every offset checked)
    assert len(pdfcheck.pages(data)) == 1
    t = pdfcheck.text(data)
    for want in ["192.0.2.0/24 outage (as seen by RIS) \\ and more", "Routes moved -> elsewhere: café ok ??",
                 "27 of 40 RIS peers changed path", "*> 192.0.2.0/24  198.51.100.1  0 64496 64500 i",
                 "Before the event (2026-09-29 18:50 UTC)", "AS64500 · event 7", "Page 1 of 1"]:
        assert want in t, want
    info = next(b for b in objs.values() if b"/Producer (bgpcanary)" in b)
    assert "Event 7: an outage → seen".encode("utf-16-be").hex().upper().encode() in info
    assert sum(b"/Subtype /Image" in b for b in objs.values()) == 1
    assert sum(b"/Subtype /Link" in b for b in objs.values()) == 1
    assert [(t, p, [k[0] for k in kids]) for t, p, kids in pdfcheck.outline(data)] == [
        ("192.0.2.0/24 outage (as seen by RIS) \\ and more", 1, []), ("Around this event", 1, ["Part of it"])]


def test_text_is_drawn_in_windows_1252():
    from bgpcanary import pdf
    assert pdf.encode("AS64500 → AS64496") == b"AS64500 -> AS64496"
    assert pdf.encode("Telefónica · 4 × 5 – ok") == "Telefónica · 4 × 5 – ok".encode("cp1252")
    assert pdf.encode("Ōsaka 日本 ≥ 3\tok") == b"Osaka ?? >= 3 ok"
    assert pdf.width("W", pdf.BOLD, 10) == 9.44 and pdf.width("i", pdf.REGULAR, 10) == 2.22
    assert pdf.width("any text", pdf.MONO, 10) == 8 * 6.0


def test_lines_wrap_within_the_width():
    from bgpcanary import pdf
    d = pdf.Doc()
    text = "a fairly ordinary sentence that goes on " * 6 + "x" * 300 + "\nits own line"
    lines = d.wrap(text, pdf.REGULAR, 9, 200)
    assert all(pdf.width(ln, pdf.REGULAR, 9) <= 200 for ln in lines)
    assert "".join(lines).replace(" ", "") == text.replace(" ", "").replace("\n", "")   # nothing lost
    assert lines[-1] == "its own line" and len(lines) > 8


def test_tables_and_blocks_carry_on_across_pages():
    from bgpcanary import pdf
    d = pdf.Doc()
    d.table([("Time", 90, "l"), ("What happened", None, "l")], [[f"18:{i:02d}", f"row {i}"] for i in range(200)])
    d.pre("\n".join(f"line {i} " + "x" * (250 if i == 3 else 10) for i in range(300)))
    d.image(png(400, 4000), caption="a tall one")   # (shrunk to fit a page)
    data = d.render()
    ps = pdfcheck.pages(data)
    assert len(ps) > 6
    with_rows = [p for p in ps if any(re.fullmatch(r"row \d+", t) for t in p)]
    assert len(with_rows) > 2 and all("What happened" in p for p in with_rows)   # the header on every page of it
    t = pdfcheck.text(data)
    assert "row 0" in t and "row 199" in t and "line 0 " in t and "line 299 " in t
    lines = [x for x in t.split("\n") if x.startswith("line 3 ") or x.startswith("    xxx")]
    assert len(lines) >= 3   # the long line went on over more lines, indented
    assert ps[-1][-1] == f"Page {len(ps)} of {len(ps)}"
