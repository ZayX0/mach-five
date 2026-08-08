"""Offline self-check for mach_five.py — quoting/skew math, no network."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mach_five import (BASE_SIZE, MAX_INVENTORY, RESIZE_FRAC, allowlist,
                       fair_value, keep_quote, quotes, shutdown,
                       side_views, size_override, step, touch_ex_self)
from us_orders import TICK, UsBook, snap_bid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_us_orders import FakeClient


def check() -> None:
    fv = 0.573
    # flat: symmetric quotes straddling fair value, equal size, sum ~ < 1
    (pa, sa), (pb, _) = quotes(fv, 0.0)
    assert pa < fv < pb, (pa, fv, pb)
    assert abs(sa - BASE_SIZE) < 1e-9
    b_cost = 1.0 - pb                  # what we'd pay for the B leg
    assert pa + b_cost < 1.0, "pair must cost < $1 to profit"

    # long A -> both quotes drop, buy less A, more B
    (pa2, sa2), (_, sb2) = quotes(fv, MAX_INVENTORY)
    assert pa2 < pa, "long A should lower the A bid"
    assert sa2 < BASE_SIZE < sb2, "should shrink loaded side, grow the other"

    # short A -> mirror
    (_, sa3), (_, sb3) = quotes(fv, -MAX_INVENTORY)
    assert sa3 > BASE_SIZE > sb3

    # fair value is team-agnostic: any devigged pair lands in (0,1)
    assert 0.0 < fair_value(-136, 124) < 1.0

    # keep_quote: the requote policy (shared with replay's policy tables)
    assert keep_quote(0.47, 40.0, 0.47, 40.0)
    assert not keep_quote(0.47, 40.0, 0.475, 40.0)     # price moved a tick
    assert not keep_quote(0.47, 40.0, 0.47, 0.0)       # skew shut side off
    assert keep_quote(0.47, 40.0, 0.47, 40.0 * (1 + RESIZE_FRAC) - 1e-6)
    assert not keep_quote(0.47, 40.0, 0.47, 90.0)      # size drifted > 25%
    assert keep_quote(0.47, 40.0, 0.47, 90.0, None)    # price-only variant

    # step() tick 1 (nothing resting): a post-only bid on each side
    # straddling fv; no cancels — there is nothing to cancel. Its return
    # is the journal's per-tick state (observability only)
    c = FakeClient()
    book = UsBook("aec-mlb-nyy-chc", long_side="B", client=c)
    res = step(book, -136, 124)
    assert not c.orders.cancels and not c.orders.single_cancels
    assert len(c.orders.created) == 2, c.orders.created
    assert res["posted"] == 2 and res["kept"] == 0 and res["open"] == 2
    assert res["sides"]["A"]["act"] == "posted" and res["sides"]["A"]["oid"]
    assert res["sides"]["A"]["since"] is not None
    a, b = c.orders.created
    assert a["intent"] == "ORDER_INTENT_BUY_SHORT"   # A=home, away is long
    assert b["intent"] == "ORDER_INTENT_BUY_LONG"
    fv2 = fair_value(-136, 124)
    # both submitted prices are in LONG terms: the A bid arrives flipped
    pa4 = 1.0 - float(a["price"]["value"])           # back to A's own price
    pb4 = float(b["price"]["value"])                 # B(long)'s own price
    assert pa4 < fv2 and pb4 < 1.0 - fv2 + 1e-9      # both bids behind fair
    assert pa4 + pb4 < 1.0, "the pair must still cost < $1 after snapping"
    assert all(o["participateDontInitiate"] for o in (a, b))

    # tick 2, sharp price unchanged: both sides KEPT — no venue churn,
    # and no orders.list verify call needed (posted == 0 -> no "open" key)
    res2 = step(book, -136, 124)
    assert len(c.orders.created) == 2 and not c.orders.single_cancels
    assert res2["kept"] == 2 and res2["posted"] == 0 and "open" not in res2
    assert res2["sides"]["B"]["act"] == "kept"
    assert res2["sides"]["B"]["since"] == res["sides"]["B"]["since"]

    # tick 3, sharp price moves: each side cancel-replaced BY ID (never
    # cancel_all — that would surrender both queue spots)
    step(book, -160, 145)
    assert len(c.orders.created) == 4
    assert set(c.orders.single_cancels) == {"O1", "O2"}
    assert not c.orders.cancels

    # a stray resting order (prior run's leftover, not tracked) is swept
    c.orders.open.append({"id": "stale", "marketSlug": "aec-mlb-nyy-chc"})
    step(book, -160, 145)
    assert "stale" in c.orders.single_cancels
    assert len(c.orders.created) == 4                # both sides still kept

    # silent post-only rejection (pilot session 1): create answers with an
    # id but the order never rests -> step's orders.list check catches it
    import mach_five as m5
    c3 = FakeClient()
    c3.orders.respond = lambda p: {"id": "ghost"}   # accepted, never rests
    book3 = UsBook("aec-mlb-nyy-chc", long_side="B", client=c3)
    old_log, logs = m5._log, []
    m5._log = logs.append
    try:
        step(book3, -136, 124)
    finally:
        m5._log = old_log
    assert any("VERIFY" in m and "0/2" in m for m in logs), logs
    # ...and is now CORRECTED, not just logged: each dropped side is
    # retried once, one tick lower (2 posts + 2 retries)
    assert len(c3.orders.created) == 4, c3.orders.created
    assert any("RETRY" in m for m in logs), logs

    # touch_bid: lift a bid to the displayed touch only while it clears
    # min_edge below fair; otherwise the desired price stands
    from mach_five import touch_bid
    assert touch_bid(0.62, 0.63, 0.625, 0.64, 0.004) == 0.625   # join
    assert touch_bid(0.62, 0.63, 0.625, 0.64, 0.006) == 0.62    # too thin
    assert touch_bid(0.62, 0.63, None, None, 0.004) == 0.62     # no book
    assert touch_bid(0.62, 0.63, 0.615, 0.64, 0.004) == 0.62    # touch below
    # improve: one tick inside the touch when edge allows...
    assert touch_bid(0.62, 0.64, 0.625, 0.65, 0.004, improve=True) == 0.63
    # ...falling back to a plain join when the improved price is too thin
    assert touch_bid(0.62, 0.63, 0.625, 0.64, 0.004, improve=True) == 0.625
    # ask cap (step() parity) binds even on the improved price
    assert touch_bid(0.62, 0.65, 0.625, 0.63, 0.004, improve=True) == 0.625

    # side_views: per-side own-frame (bids ladder, best ask) from the raw
    # long-frame book; the short side's view is the mirror
    raw = {"bids": [(0.44, 5.0)], "asks": [(0.5, 7.0)]}
    assert side_views(raw, "A") == {"A": ([(0.44, 5.0)], 0.5),
                                    "B": ([(0.5, 7.0)], 0.56)}
    assert side_views(raw, "B") == {"A": ([(0.5, 7.0)], 0.56),
                                    "B": ([(0.44, 5.0)], 0.5)}
    assert side_views(None, "A") == {"A": ([], None), "B": ([], None)}

    # touch_ex_self: the joinable touch excludes our own resting order
    assert touch_ex_self([(0.56, 21.0)], 0.56, 21) is None      # only us
    assert touch_ex_self([(0.56, 22.0)], 0.56, 21) == 0.56      # not just us
    assert touch_ex_self([(0.56, 21.0), (0.555, 40.0)], 0.56, 21) == 0.555
    assert touch_ex_self([(0.56, 21.0)], None, 0) == 0.56       # not resting
    assert touch_ex_self([], None, 0) is None

    # step() + raw_book, long_side B, fv(-136,124) ~ 0.5635: desired A
    # 0.5575 snaps to 0.555, one tick behind the displayed 0.56 touch,
    # which keeps >= JOIN_EDGE vs fair -> join at 0.560. B's touch (0.425)
    # sits below its desired 0.430 -> plain fv price
    c4 = FakeClient()
    book4 = UsBook("aec-mlb-nyy-chc", long_side="B", client=c4)
    raw4 = {"bids": [(0.425, 50.0)], "asks": [(0.44, 50.0)]}
    res4 = step(book4, -136, 124, raw4)
    assert res4["sides"]["A"]["px"] == 0.56, res4["sides"]["A"]
    assert res4["sides"]["B"]["px"] == 0.43, res4["sides"]["B"]
    # same book next tick: the joined quote is unchanged and KEPT (our own
    # 21 contracts leave 29 of the displayed 50 at the touch)
    res4b = step(book4, -136, 124, raw4)
    assert res4b["sides"]["A"]["act"] == "kept"
    assert res4b["sides"]["A"]["px"] == 0.56
    # everyone else leaves the touch (displayed 21 = exactly our order):
    # the self-netted touch vanishes and the bid falls back to fv pricing
    res4c = step(book4, -136, 124,
                 {"bids": [(0.425, 50.0)], "asks": [(0.44, 21.0)]})
    assert res4c["sides"]["A"]["act"] == "posted"
    assert res4c["sides"]["A"]["px"] == 0.555, res4c["sides"]["A"]

    # ask cap (fix 1): a bid that would sit at/through its ask is lowered
    # to one tick below it — the venue silently drops would-cross posts
    c4b = FakeClient()
    book4b = UsBook("aec-mlb-nyy-chc", long_side="B", client=c4b)
    (wa, _), _ = quotes(fair_value(-136, 124), 0.0)
    want_a = snap_bid(wa)                       # 0.555
    raw_cap = {"bids": [(1.0 - want_a, 9.0)],   # long bid = A ask AT our px
               "asks": [(0.45, 9.0)]}
    res4d = step(book4b, -136, 124, raw_cap)
    assert res4d["sides"]["A"]["px"] == snap_bid(want_a - TICK)
    assert res4d["sides"]["A"]["act"] == "posted"

    # blocked side (fix 3, mid-cooldown after a one-sided tape pull):
    # sized to zero — resting order canceled, nothing posts — while the
    # other side keeps quoting and its queue spot
    c6 = FakeClient()
    book6 = UsBook("aec-mlb-nyy-chc", long_side="B", client=c6)
    r6 = step(book6, -136, 124)
    a_oid = r6["sides"]["A"]["oid"]
    r6b = step(book6, -136, 124, None, ("A",))
    assert r6b["sides"]["A"]["act"] == "none" and r6b["blocked"] == ["A"]
    assert a_oid in c6.orders.single_cancels        # resting A pulled
    assert r6b["sides"]["B"]["act"] == "kept"       # B held its queue spot
    r6c = step(book6, -136, 124)                    # cooldown over
    assert r6c["sides"]["A"]["act"] == "posted" and "blocked" not in r6c

    # retry detail: the reposted bid is one tick below the original and
    # the journal side carries the retry marker
    c5 = FakeClient()
    c5.orders.respond = lambda p: {"id": "ghost"}   # accepted, never rests
    book5 = UsBook("aec-mlb-nyy-chc", long_side="B", client=c5)
    m5._log = logs.append
    try:
        res5 = step(book5, -136, 124)
    finally:
        m5._log = old_log
    want5 = {"A": res5["sides"]["A"], "B": res5["sides"]["B"]}
    (wa5, _), (wb5, _) = quotes(fair_value(-136, 124), 0.0)
    assert want5["A"]["retry"] == 1 and want5["B"]["retry"] == 1
    assert want5["A"]["px"] == snap_bid(snap_bid(wa5) - TICK)
    assert want5["B"]["px"] == snap_bid(snap_bid(1.0 - wb5) - TICK)

    # Journal: appends JSONL; a broken path must be silently tolerated
    import json as _json
    import tempfile
    from mach_five import Journal
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(path=str(Path(tmp) / "j.jsonl"))
        j.write({"type": "tick", "ts": 1.0, "slug": "s", "quoting": True})
        j.write({"type": "pull", "ts": 2.0, "slug": "s", "reason": "r"})
        lines = [_json.loads(l) for l in
                 (Path(tmp) / "j.jsonl").read_text().splitlines()]
        assert [l["type"] for l in lines] == ["tick", "pull"], lines
    Journal(path="/nonexistent-dir/x/y.jsonl").write({"a": 1})  # no raise

    # size_override(): both-or-neither, ratio-locked to the coded 2:5
    for k in ("MACH_FIVE_BASE", "MACH_FIVE_MAX"):
        os.environ.pop(k, None)
    assert size_override() is None
    os.environ["MACH_FIVE_BASE"] = "20"
    os.environ["MACH_FIVE_MAX"] = "50"
    assert size_override() == (20.0, 50.0)
    for bad in ({"MACH_FIVE_BASE": "20", "MACH_FIVE_MAX": "60"},   # ratio
                {"MACH_FIVE_BASE": "20"},                          # lone var
                {"MACH_FIVE_BASE": "x", "MACH_FIVE_MAX": "50"}):   # garbage
        for k in ("MACH_FIVE_BASE", "MACH_FIVE_MAX"):
            os.environ.pop(k, None)
        os.environ.update(bad)
        try:
            size_override()
            raise AssertionError(f"accepted bad override {bad}")
        except SystemExit:
            pass
    for k in ("MACH_FIVE_BASE", "MACH_FIVE_MAX"):
        os.environ.pop(k, None)

    # allowlist(): the pilot's one-game restriction, parsed from the env
    os.environ["MACH_FIVE_SLUGS"] = " aec-mlb-a-b , aec-mlb-c-d ,"
    assert allowlist() == {"aec-mlb-a-b", "aec-mlb-c-d"}
    os.environ["MACH_FIVE_SLUGS"] = ""
    assert allowlist() == set()      # empty/unset = no restriction
    del os.environ["MACH_FIVE_SLUGS"]
    assert allowlist() == set()

    # shutdown(): every tracked market gets a cancel; one failure must not
    # stop the others (an exit that skips a cancel leaves an orphan order)
    class Q:
        def __init__(self, book):
            self.book = book

    class Boom:
        slug, net_a = "boom", 0.0
        def cancel_all(self):
            raise RuntimeError("venue timeout")

    c2 = FakeClient()
    ok_book = UsBook("aec-mlb-ok", long_side="A", client=c2)
    logs: list[str] = []
    shutdown({"aec-mlb-ok": Q(ok_book), "boom": Q(Boom())},
             log=logs.append)
    assert len(c2.orders.cancels) == 1           # the healthy book canceled
    assert any("CANCEL FAILED boom" in m for m in logs), logs
    assert any("canceled aec-mlb-ok" in m for m in logs), logs
    print("ok")


if __name__ == "__main__":
    check()
