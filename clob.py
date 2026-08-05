"""Polymarket client.

READ path mirrors mlb-prop-finder/scripts/probe_exchanges.py: keyless HTTP
against Gamma (event/market discovery) and the CLOB (`GET /book?token_id=`).
That's all that repo does — it never places orders.

WRITE path (post/cancel) needs L2 auth + EIP-712 order signing, which the
reference repo does not do. Use the official `py-clob-client` SDK for it
rather than hand-rolling signing — the stubs below mark exactly where it slots
in. `pip install py-clob-client`, then a signed ClobClient with your funder
key drives create/post/cancel.
"""
from __future__ import annotations

import json
from typing import Any

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _get(url: str, **params) -> Any:
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _jsonish(value: Any) -> Any:
    """Gamma returns outcomes / clobTokenIds as JSON strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# --- READ: order book (real, keyless) -------------------------------------
def order_book(token_id: str) -> dict[str, Any]:
    """Best bid/ask + sorted levels for one YES token (CLOB `GET /book`)."""
    book = _get(f"{CLOB}/book", token_id=str(token_id))
    bids = sorted(_levels(book.get("bids", [])), reverse=True)
    asks = sorted(_levels(book.get("asks", [])))
    return {
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "bids": bids,
        "asks": asks,
    }


def _levels(entries: list[dict]) -> list[tuple[float, float]]:
    out = []
    for e in entries or []:
        try:
            out.append((float(e["price"]), float(e["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# --- WRITE: order placement (STUB — needs py-clob-client) ------------------
def place_order(token_id: str, price: float, size: float) -> str:
    """Post a signed limit BUY of `size` YES @ `price` on `token_id`.
    ponytail: stub — wire to py-clob-client (ClobClient.create_and_post_order
    with a signed OrderArgs); returns the order id."""
    raise NotImplementedError("order placement needs an authed py-clob-client")


def cancel_all(token_id: str | None = None) -> None:
    """Cancel our resting orders (all, or one token).
    ponytail: stub — ClobClient.cancel_all() / cancel_market_orders()."""
    raise NotImplementedError("order cancel needs an authed py-clob-client")
