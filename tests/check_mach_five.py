"""Offline self-check for mach_five.py — quoting/skew math, no network."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mach_five import (BASE_SIZE, MAX_INVENTORY, allowlist, fair_value,
                       quotes, shutdown, step)
from us_orders import UsBook

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

    # step(): one cancel-replace cycle through the real order layer (fake
    # client) — cancel first, then a post-only bid on each side straddling fv
    c = FakeClient()
    book = UsBook("aec-mlb-nyy-chc", long_side="B", client=c)
    step(book, -136, 124)
    assert len(c.orders.cancels) == 1
    assert len(c.orders.created) == 2, c.orders.created
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
