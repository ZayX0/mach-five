"""Offline self-check for us_orders.py — injected fake client, no network,
no real orders."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from us_orders import (UsBook, cancel_market, cancel_order, create_rejected,
                       net_a_shares, open_orders, place_bid, snap_bid)


class FakeOrders:
    """Fake venue order book: create rests an order (unless `respond`
    overrides the response), cancel_all clears a market, list shows what
    rests — mirroring that orders.list is the authority."""

    def __init__(self, respond=None):
        self.created, self.cancels, self.single_cancels = [], [], []
        self.open = []
        self.respond = respond or (lambda params: None)

    def create(self, params):
        self.created.append(params)
        canned = self.respond(params)
        if canned is not None:
            return canned              # e.g. a silent post-only rejection
        oid = f"O{len(self.created)}"
        self.open.append(dict(params, id=oid))
        return {"id": oid}

    def cancel_all(self, params=None):
        self.cancels.append(params)
        slugs = (params or {}).get("slugs", [])
        ids = [o["id"] for o in self.open if o["marketSlug"] in slugs]
        self.open = [o for o in self.open if o["marketSlug"] not in slugs]
        return {"canceledOrderIds": ids}

    def cancel(self, order_id, params=None):
        self.single_cancels.append(order_id)
        self.open = [o for o in self.open if o["id"] != order_id]

    def list(self):
        return {"orders": self.open}


class FakePortfolio:
    def __init__(self, positions):
        self._positions = positions

    def positions(self, params=None):
        return {"positions": self._positions}


class FakeClient:
    def __init__(self, positions=None):
        self.orders = FakeOrders()
        self.portfolio = FakePortfolio(positions or {})


def check() -> None:
    assert snap_bid(0.472) == 0.47 and snap_bid(0.475) == 0.475
    assert snap_bid(0.5199) == 0.515

    c = FakeClient()
    # A-frame -> intent mapping: buying A when away is long = BUY_SHORT.
    # The venue prices in LONG terms (proof order 2026-08-02), so a short
    # buy at own-price 0.47 submits (1 - 0.47) = 0.53
    oid = place_bid(c, "aec-mlb-nyy-chc", long_side="B", side="A",
                    price=0.472, dollars=100.0)
    assert oid == "O1"
    o = c.orders.created[0]
    assert o["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert o["price"] == {"value": "0.5300", "currency": "USD"}
    assert o["quantity"] == int(100.0 / 0.47) == 212   # sized at OUR price
    assert o["type"] == "ORDER_TYPE_LIMIT"
    assert o["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    assert o["participateDontInitiate"] is True     # post-only, always

    # buying the long side = BUY_LONG at its own (= long-frame) price
    place_bid(c, "aec-mlb-nyy-chc", long_side="B", side="B",
              price=0.516, dollars=100.0)
    assert c.orders.created[1]["intent"] == "ORDER_INTENT_BUY_LONG"
    assert c.orders.created[1]["price"]["value"] == "0.5150"

    # degenerate orders never reach the venue
    assert place_bid(c, "s", "A", "A", price=0.0004, dollars=50.0) is None
    assert place_bid(c, "s", "A", "A", price=0.47, dollars=0.30) is None
    assert len(c.orders.created) == 2

    # orders.list is the authority on what rests, scoped per market
    assert len(open_orders(c, "aec-mlb-nyy-chc")) == 2
    assert open_orders(c, "some-other-market") == []

    # cancel is scoped to ONE market
    ids = cancel_market(c, "aec-mlb-nyy-chc")
    assert ids == ["O1", "O2"]
    assert c.orders.cancels[0] == {"slugs": ["aec-mlb-nyy-chc"]}
    assert open_orders(c, "aec-mlb-nyy-chc") == []

    # create_rejected: dead-sounding status or missing id = not resting
    assert create_rejected({"id": "X"}) is None
    assert create_rejected({"order": {"id": "X", "status": None}}) is None
    r = create_rejected({"id": "X", "status": "ORDER_STATUS_REJECTED"})
    assert r == "ORDER_STATUS_REJECTED"
    assert create_rejected({"order": {"status": "CANCELED"}}) == "CANCELED"
    assert "no order id" in create_rejected({})
    assert "unexpected" in create_rejected("garbage")

    # a rejecting venue: place_bid returns None and logs the reason
    logs = []
    cr = FakeClient()
    cr.orders.respond = lambda p: {"id": "X", "status": "ORDER_STATUS_REJECTED"}
    oid = place_bid(cr, "aec-mlb-nyy-chc", "B", "A", price=0.472,
                    dollars=100.0, log=logs.append)
    assert oid is None
    assert logs and "NOT resting" in logs[0] and "REJECTED" in logs[0], logs

    # positions: netPosition is in LONG-side terms; flip for away-long
    c2 = FakeClient({"aec-mlb-nyy-chc": {"netPosition": "-12.5"}})
    assert net_a_shares(c2, "aec-mlb-nyy-chc", long_side="B") == 12.5
    assert net_a_shares(c2, "aec-mlb-nyy-chc", long_side="A") == -12.5
    assert net_a_shares(c2, "no-such-market", "A") == 0.0

    # UsBook wires it together and converts to mach_five's $ convention
    b = UsBook("aec-mlb-nyy-chc", "B", client=c2)
    b.poll_fills()
    assert b.net_a == 12.5                      # short the long(away) = long A
    assert abs(b.inventory_dollars(0.48) - 12.5 * 0.48) < 1e-9
    b.net_a = -10.0
    assert abs(b.inventory_dollars(0.48) + 10.0 * 0.52) < 1e-9
    b.post("A", 0.472, 100.0)
    # post tracks the resting bid (id, SNAPPED price, $) for the keep policy
    assert b.resting["A"] == {"id": "O1", "price": 0.47, "dollars": 100.0}
    b.cancel_all()
    assert len(c2.orders.created) == 1 and len(c2.orders.cancels) == 1
    assert b.resting == {}                      # cancel_all drops tracking

    # single-order cancel hits the by-id endpoint with the market slug
    c3 = FakeClient()
    oid = place_bid(c3, "aec-mlb-x-y", "A", "A", price=0.47, dollars=50.0)
    cancel_order(c3, "aec-mlb-x-y", oid)
    assert c3.orders.single_cancels == [oid]
    assert open_orders(c3, "aec-mlb-x-y") == []

    # keep-policy plumbing: per-side cancel keeps the other side; open_ids
    # is the authority the keep decision checks tracking against
    c4 = FakeClient()
    b4 = UsBook("aec-mlb-x-y", "A", client=c4)
    oid_a = b4.post("A", 0.472, 100.0)
    oid_b = b4.post("B", 0.514, 100.0)
    ids = b4.open_ids()
    assert ids == {oid_a, oid_b}
    b4.cancel_side("A", ids)
    assert c4.orders.single_cancels == [oid_a]
    assert "A" not in b4.resting and b4.open_ids() == {oid_b}
    b4.cancel_side("A", ids)          # nothing tracked: no venue call
    assert c4.orders.single_cancels == [oid_a]
    # a tracked order that no longer rests is dropped without a venue call
    b4.resting["B"]["id"] = "gone"
    b4.cancel_side("B", b4.open_ids())
    assert c4.orders.single_cancels == [oid_a] and b4.resting == {}
    # strays (resting but untracked — a prior run's leftovers) get swept
    assert b4.cancel_strays(b4.open_ids()) == 1     # oid_b, orphaned above
    assert b4.open_ids() == set() and b4.cancel_strays(b4.open_ids()) == 0
    # a failed post clears the side's tracking instead of leaving a ghost
    c4.orders.respond = lambda p: {"id": "X", "status": "ORDER_STATUS_REJECTED"}
    assert b4.post("A", 0.472, 100.0) is None and "A" not in b4.resting
    print("ok")


if __name__ == "__main__":
    check()
