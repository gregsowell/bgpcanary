"""The connectivity-tree pictures in alert emails (tree.render), labelled as the dashboard's tree (static/replay.js)
labels them, for every state a branch can be in. Among them: one of your upstreams that carried none of your routes
before the event and still carries none when the alert goes out (down for hours, or configured but not live yet).
Up to 1.7.9 drawing that upstream failed (KeyError 'of'), and the alert went out without its pictures.

No network: the paths are made up and RIPEstat's neighbour lists are replaced. Documentation AS numbers only, with
tests/conftest.py's config: you are AS64500, your upstreams are AS64496 and AS64497, your peer is AS64498."""
import asyncio
import dataclasses

import pytest
from PIL import ImageDraw

from conftest import CONFIG, NAMES, NOW, P1

YOU, U1, U2, PEER = 64500, 64496, 64497, 64498
PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def drawn(monkeypatch):
    """Every text the pictures write, in order."""
    out = []
    text = ImageDraw.ImageDraw.text

    def spy(self, xy, s, *a, **kw):
        out.append(s)
        return text(self, xy, s, *a, **kw)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return out


def via(n, *hops):
    """`n` RIS peers' paths (origin last) through `hops`, the RIS peer's own network first."""
    return [[*hops, YOU] for _ in range(n)]


def test_an_upstream_that_carried_nothing_is_drawn(drawn):
    """AS64497 carried none of your routes before the event and carries none now: drawn as the dashboard draws it,
    "not carrying you" (it's your own upstream: no "via"), and so is its transit AS64511, which RIS sees carrying you
    nowhere."""
    from bgpcanary import tree
    before, now = via(10, 65001, 64510, U1), via(4, 65001, 64510, U1)
    t = tree.Tree(YOU, [before, now], [U1, U2], {U1: [(64510, 0)], U2: [(64511, 0)]}, known={U1, U2, PEER})
    for paths in (before, now):   # the email's "before" picture compares the moment with itself
        drawn.clear()
        assert tree.render(t, NAMES, paths, before, 10, "Now", P1).startswith(PNG)
        assert "AS64497 UPSTREAM-TWO" in drawn
        assert drawn.count("not carrying you") == 2   # the legend's, and AS64497's
        assert "not carrying you via AS64497" in drawn
    assert drawn.count("4 of 10") == 3   # AS64496, AS64510 behind it and the RIS peer's AS65001


def test_a_new_upstream_on_the_before_picture(drawn):
    """An upstream that turns up during the event (what an "unexpected upstream" alert is about) carried nothing at the
    "before" moment: that picture draws it as not carrying you, the one after it as new."""
    from bgpcanary import tree
    before = via(10, 65001, 64510, U1) + via(5, 65002, 64511, U2)
    now = before + via(3, 65003, 64512, 64499)
    t = tree.Tree(YOU, [before, now], [U1, U2, 64499], {}, known={U1, U2, PEER})
    assert tree.render(t, NAMES, before, before, 18, "Before", P1).startswith(PNG)
    assert drawn.count("not carrying you") == 2   # the legend's, and AS64499's
    drawn.clear()
    assert tree.render(t, NAMES, now, before, 18, "Now", P1).startswith(PNG)
    assert "AS64499 STRANGER" in drawn and "3 (new)" in drawn


def test_every_branch_state_is_drawn(drawn):
    """One picture with a branch in each state: degraded, dropped, moved, new, not carrying you, reaching you
    directly or another way, no longer peered, idle (only seen at the worst point), "+N more", exchanges."""
    from bgpcanary import tree
    ix = lambda a: "IX-ONE" if a == PEER else None   # noqa: E731 (peering.Peering.group)
    small = [p for a in range(64520, 64526) for p in via(1, 65009, a, U1)]   # six more transits behind AS64496
    before = tree.group_peers(via(10, 65001, 64510, U1) + via(3, 65002, 64512, U1) + via(5, 65003, 64513, U1)
                              + via(2, 65004, PEER) + small, YOU, ix)
    worst = tree.group_peers(via(2, 65006, 64517, U2), YOU, ix)
    now = tree.group_peers(via(4, 65001, 64510, U1) + via(5, 65003, 64513, U2) + via(1, 65005, 64514, U2)
                           + via(2, 65004, PEER) + small, YOU, ix)
    nbrs = {U1: [(64510, 0), (64515, 0), (PEER, 0)], U2: [(64511, 0), (64510, 0), (64513, 0)]}
    gone = {U1: [(64516, {"first": NOW - 20 * 86400, "last": NOW - 86400, "peak": 30, "peak_at": NOW - 86400})]}
    t = tree.Tree(YOU, [before, worst, now], [U1, U2], nbrs, gone=gone, known={U1, U2, PEER})
    assert tree.render(t, NAMES, now, before, 26, "Now", P1).startswith(PNG)
    for label in ("UPSTREAMS · EXCHANGES", "4 of 10",                     # AS64510 behind AS64496
                  "0 (was 3)",                                            # AS64512: dropped
                  "moved to via AS64497", "5 (new)",                      # AS64513: from AS64496 to AS64497
                  "1 (new)",                                              # AS64514
                  "not carrying you via AS64496", "not carrying you via AS64497",   # AS64515, AS64511
                  "reaches you directly",                                 # AS64498 behind AS64496: your peer
                  "reaches you another way",                              # AS64510 behind AS64497
                  "AS64517",                                              # idle: only there at the worst point
                  "+2 more", "Exchanges", "IX-ONE"):
        assert label in drawn, label
    assert drawn.count("no longer peered") == 2   # the legend's, and AS64516's
    assert drawn.count("not carrying you") == 1   # the legend's only: both upstreams carry you now

    drawn.clear()   # the same with the email's choice of folding those that reach you another way into one line
    t = tree.Tree(YOU, [before, worst, now], [U1, U2], nbrs, gone=gone, known={U1, U2, PEER}, fold=True)
    assert tree.render(t, NAMES, now, before, 26, "Now", P1).startswith(PNG)
    assert drawn.count("+1 reach you another way") == 2
    assert "reaches you directly" not in drawn and "reaches you another way" not in drawn


@pytest.mark.parametrize("phase, pictures", [("open", ["tree-before", "tree-now"]),
                                             ("close", ["tree-before", "tree-after"])])
def test_alert_pictures_with_an_upstream_down_for_hours(monkeypatch, tmp_path, phase, pictures):
    """The alert email's pictures when your second upstream has carried nothing in the three hours before the event:
    on 1.7.9 they failed and the email went out without them."""
    from bgpcanary import config, db, tree
    cfg = dataclasses.replace(config.load(CONFIG), prefixes=[P1], db_path=str(tmp_path / "tree.db"))
    db.init(cfg.db_path)
    start = NOW - 600
    monkeypatch.setattr(tree, "state_paths",
                        lambda conn, prefix, ts: via(10 if ts < start else 3, 65001, 64510, U1))

    async def neighbours(sourceapp, ups):
        assert sorted(ups) == [U1, U2]   # both configured upstreams, though AS64497 is on no path
        return {U1: [(64510, 0)], U2: [(64511, 0)]}
    monkeypatch.setattr(tree, "upstream_neighbors", neighbours)
    ev = {"kind": "outage", "prefix": P1, "start_ts": start, "end_ts": NOW - 60 if phase == "close" else None}
    out = asyncio.run(tree.event_images(cfg, None, ev, phase, now=NOW))
    assert [cid for cid, _, _ in out] == pictures
    assert all(png.startswith(PNG) for _, png, _ in out)
