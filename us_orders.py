"""Polymarket US order layer — the venue's WRITE path (place/cancel/positions).

Everything speaks the repo's A=home frame. "Buy A YES at p" becomes an order
on the game's single US instrument with the right INTENT — BUY_LONG when the
long team is the home team (long_side "A"), BUY_SHORT when away.

PRICE FRAME (live-verified by the $1 proof order, 2026-08-02): the venue
prices EVERY order in the LONG instrument's terms. A BUY_SHORT rests as a
SELL of the long side at the submitted price — so buying the short side at
its own price p must submit (1 - p). Submitting p unverified would have
quoted ~2p-1 away from the intended level; the proof order (BMJPF7YWGBA3)
caught it: BUY_SHORT @ 0.465 landed at long-frame ask 0.465, not 0.535.

Safety rails baked in:
- LIMIT + GTC + participateDontInitiate (post-only): an order that would
  cross the book is rejected by the venue instead of taking — the maker
  can never accidentally pay the spread.
- Bids snap DOWN to the half-cent grid (never tighter than intended).
- Quantity is floored to whole contracts; less than one contract = no order.

SILENT REJECTION (pilot session 1, 2026-08-04): the venue rejects a
would-cross post-only order WITHOUT raising — orders.create returns
normally and the order simply never rests. While Pinnacle led the venue by
more than the half-spread (~1h that session), one side's bid was silently
absent and the loop believed both sides were up. Two-layer defense here:
`create_rejected` inspects every create response (cheap, best-effort — the
rejection response shape is not fully mapped), and `open_orders` lets the
caller verify what actually rests (orders.list is the authority).

Auth: the official `polymarket_us` SDK (Ed25519-signed requests), creds from
the macOS Keychain via us_market.creds() — never .env, never logs.
Offline check: tests/check_us_orders.py (injected fake client; no network,
no real orders).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import us_market

TICK = 0.005  # orderPriceMinTickSize on US MLB moneylines


def snap_bid(price: float) -> float:
    """A bid snapped DOWN to the venue tick grid — the conservative
    direction (never quote tighter than intended)."""
    return round(math.floor(price / TICK + 1e-9) * TICK, 4)


def _client():
    from polymarket_us import PolymarketUS
    key_id, secret = us_market.creds()
    return PolymarketUS(key_id=key_id, secret_key=secret)


def _amount(price: float) -> dict:
    return {"value": f"{price:.4f}", "currency": "USD"}


_DEAD_WORDS = ("REJECT", "CANCEL", "EXPIRE", "FAIL")


def create_rejected(resp) -> str | None:
    """Reason string when an orders.create response shows the order did NOT
    rest, else None. Best-effort: the venue's rejection response shape is
    not fully mapped (a resting order's list row even carries status null),
    so this flags what it can — a dead-sounding status or a missing id —
    and `open_orders` remains the authority on what actually rests."""
    if not isinstance(resp, dict):
        return f"unexpected response type {type(resp).__name__}"
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    status = str(order.get("status") or "")
    if any(w in status.upper() for w in _DEAD_WORDS):
        return status
    if not (order.get("id") or resp.get("id")):
        return f"no order id (status {status or 'unset'})"
    return None


def place_bid(client, slug: str, long_side: str, side: str,
              price: float, dollars: float, log=None) -> str | None:
    """Rest a post-only BUY of $`dollars` of `side` YES at `price` (A-frame).
    Returns the order id, or None for a degenerate order (snapped out of
    (0,1), or under one whole contract) or a create response that shows
    the order didn't rest (logged via `log` when provided)."""
    p = snap_bid(price)
    if not (0.0 < p < 1.0):
        return None
    qty = int(dollars / p)
    if qty < 1:
        return None
    buying_long = side == long_side
    # the venue prices every order in LONG terms: buying the short side at
    # its own price p == selling the long side at (1 - p). Snapping p DOWN
    # (conservative for our bid) lands exactly on the grid after the flip.
    px = p if buying_long else round(1.0 - p, 4)
    resp = client.orders.create({
        "marketSlug": slug,
        "intent": ("ORDER_INTENT_BUY_LONG" if buying_long
                   else "ORDER_INTENT_BUY_SHORT"),
        "type": "ORDER_TYPE_LIMIT",
        "price": _amount(px),
        "quantity": qty,
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "participateDontInitiate": True,
    })
    reason = create_rejected(resp)
    if reason:
        if log:
            log(f"order NOT resting {slug} {side}@{p} x{qty}: {reason}")
        return None
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    return order.get("id") or resp.get("id")


def open_orders(client, slug: str) -> list[dict]:
    """Orders currently resting on ONE market. orders.list is the
    authority (the public book lags order changes ~10-20s)."""
    resp = client.orders.list()
    orders = resp.get("orders", []) if isinstance(resp, dict) else []
    return [o for o in orders if o.get("marketSlug") == slug]


def cancel_market(client, slug: str) -> list[str]:
    """Cancel every resting order on ONE market; returns canceled ids."""
    resp = client.orders.cancel_all({"slugs": [slug]})
    return resp.get("canceledOrderIds", []) if isinstance(resp, dict) else []


def net_a_shares(client, slug: str, long_side: str) -> float:
    """Signed A-frame net contracts for one market (+ = long home team).
    netPosition is signed in the LONG side's terms; flip when away is long.
    ponytail: assumes the positions dict is keyed by market slug — the $1
    proof order (NEXT_STEPS step 7) verifies before real size."""
    resp = client.portfolio.positions({"market": slug})
    pos = (resp.get("positions") or {}).get(slug) if isinstance(resp, dict) else None
    if not pos:
        return 0.0
    try:
        net = float(pos.get("netPosition") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return net if long_side == "A" else -net


@dataclass
class UsBook:
    """One game's US market from mach_five's point of view: A-frame
    post/cancel plus real inventory from the portfolio endpoint."""
    slug: str
    long_side: str        # 'A' home-long / 'B' away-long
    client: object = None  # lazy real client; injectable in checks
    net_a: float = 0.0    # signed contracts, + = long A
    log: object = None    # callable for order-lifecycle warnings

    def _c(self):
        if self.client is None:
            self.client = _client()
        return self.client

    def cancel_all(self) -> None:
        cancel_market(self._c(), self.slug)

    def post(self, side: str, price: float, dollars: float) -> str | None:
        return place_bid(self._c(), self.slug, self.long_side,
                         side, price, dollars, log=self.log)

    def open_count(self) -> int:
        """How many of our orders actually rest on this market right now."""
        return len(open_orders(self._c(), self.slug))

    def poll_fills(self) -> None:
        self.net_a = net_a_shares(self._c(), self.slug, self.long_side)

    def inventory_dollars(self, fv: float) -> float:
        """Signed $ of the unhedged leg, mach_five's convention (+ = long A).
        Complete sets net out: only |net_a| carries risk."""
        return self.net_a * fv if self.net_a >= 0 else self.net_a * (1.0 - fv)
