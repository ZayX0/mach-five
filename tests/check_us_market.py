"""Offline self-check for us_market.py — canned payloads, no network, no threads."""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds_feed import Game
from us_market import (WINNER_TYPE, TradeFeed, book_a_frame, find_market,
                       long_side, mirror, trade_line)


def check() -> None:
    dt = datetime(2026, 8, 1, 23, 15, tzinfo=timezone.utc)
    game = Game("Chicago Cubs", "New York Yankees", -136, 124, dt)

    ev = {"id": "1", "closed": False, "title": "New York Yankees vs Chicago Cubs",
          "startTime": "2026-08-01T23:15:00Z",
          "markets": [
              {"slug": "tsc-mlb-nyy-chc-2026-08-01-8pt5",
               "sportsMarketType": "baseball_team_full_game_total"},
              {"slug": "aec-mlb-nyy-chc-2026-08-01",
               "sportsMarketType": WINNER_TYPE, "closed": False},
          ]}
    ev_g2 = dict(ev, id="2", startTime="2026-08-02T04:15:00Z")  # doubleheader G2
    detail = {"slug": "aec-mlb-nyy-chc-2026-08-01",
              "marketSides": [
                  {"long": True, "description": "New York Yankees",
                   "team": {"name": "New York Yankees", "ordering": "away"}},
                  {"long": False, "description": "Chicago Cubs",
                   "team": {"name": "Chicago Cubs", "ordering": "home"}},
              ]}

    def fake_get(url, **params):
        if url.endswith("/v1/search"):
            return {"events": [ev, ev_g2]}
        if url.endswith("/v1/markets"):
            assert params.get("slug") == "aec-mlb-nyy-chc-2026-08-01"
            return {"markets": [detail]}
        raise AssertionError(url)

    m = find_market(game, get_json=fake_get)
    assert m is detail, m                      # picked G1 by startTime, not G2
    assert long_side(m, game.home) == "B"      # long team = Yankees = away
    no_match = find_market(Game("A Team", "B Team", 100, -100, dt),
                           get_json=lambda url, **p: {"events": []})
    assert no_match is None

    # book normalization: long==B means A's book mirrors the instrument book
    raw = {"bids": [(0.40, 100.0), (0.39, 50.0)], "asks": [(0.42, 80.0)],
           "state": "OPEN", "stats": {"sharesTraded": "5"}}
    a = book_a_frame(raw, "B")
    assert (a["best_bid"], a["best_ask"]) == (0.58, 0.60), a
    assert a["bids"] == [(0.58, 80.0)] and a["asks"] == [(0.6, 100.0), (0.61, 50.0)]
    same = book_a_frame(raw, "A")
    assert same["best_bid"] == 0.40 and same["best_ask"] == 0.42
    b = mirror(a)
    assert (b["best_bid"], b["best_ask"]) == (0.40, 0.42)  # back to instrument frame

    # trade normalization, long==B (instrument = away/Yankees YES)
    ws_trade = {"price": {"value": "0.4050"}, "quantity": {"value": "8.2000"},
                "tradeTime": "2026-08-01T04:22:43.942698741Z",
                "taker": {"side": "ORDER_SIDE_SELL"}, "id": "T1"}
    line = trade_line(ws_trade, "B", "tokA", "tokB")
    assert line["token"] == "tokB" and line["price"] == 0.405  # seller hit B book
    assert line["taker_side"] == "SELL" and line["size"] == 8.2
    assert abs(line["ts"] - 1785558163.942698) < 1e-3, line["ts"]

    buy = trade_line(dict(ws_trade, taker={"side": "ORDER_SIDE_BUY"}, id="T2"),
                     "B", "tokA", "tokB")
    assert buy["token"] == "tokA" and abs(buy["price"] - 0.595) < 1e-9  # complement
    assert trade_line({"bad": 1}, "A", "a", "b") is None

    # TradeFeed.watch dedups; no network touched
    feed = TradeFeed("k", "s", on_trade=lambda t: None, log=lambda m: None)
    assert feed.age(1000.0) == float("inf")   # never heard anything = stale
    feed.last_rx = 990.0
    assert feed.age(1000.0) == 10.0
    feed.watch({"x", "y"})
    assert feed._dirty and feed._slugs == {"x", "y"}
    feed._dirty = False
    feed.watch({"y", "x"})
    assert not feed._dirty                     # unchanged set -> no resubscribe

    # session watchdog: a closed or silent socket must tear the session down
    # (the SDK swallows ConnectionClosed, so the loop has to notice itself)
    ce = TradeFeed._conn_error
    assert ce(True, last_rx=100.0, now=100.0, stale_sec=180.0) is None
    assert ce(True, 100.0, 279.0, 180.0) is None       # quiet but within limit
    assert ce(False, 100.0, 100.0, 180.0) == "connection closed"
    stale = ce(True, 100.0, 281.0, 180.0)
    assert stale is not None and "181" in stale, stale

    # keepalive: quiet market != dead socket — a pong is life, a timeout is
    # death, and recent traffic means no probe at all
    class FakeConn:
        def __init__(self, pong_delay=0.0):
            self.pong_delay = pong_delay
            self.pings = 0

        async def ping(self):
            self.pings += 1
            return asyncio.sleep(self.pong_delay)  # awaitable "pong waiter"

    async def run_keepalive():
        now = 1000.0
        feed.PING_TIMEOUT_SEC = 0.05

        conn = FakeConn()
        feed.last_rx = now - 5.0          # recent traffic: no probe
        await feed._keepalive(conn, now)
        assert conn.pings == 0 and feed.last_rx == now - 5.0

        feed.last_rx = now - 30.0         # quiet + pong: life, last_rx fresh
        await feed._keepalive(conn, now)
        assert conn.pings == 1 and feed.last_rx > now - 30.0

        feed.last_rx = now - 30.0         # quiet + pong timeout: tear down
        slow = FakeConn(pong_delay=10.0)
        try:
            await feed._keepalive(slow, now)
            raise AssertionError("unanswered ping must raise")
        except ConnectionError as e:
            assert "30s" in str(e), e

        feed.last_rx = now - 30.0         # no connection object yet: no-op
        await feed._keepalive(None, now)
        assert feed.last_rx == now - 30.0

    asyncio.run(run_keepalive())

    # creds(): backend selection — systemd credentials dir when present
    # (Linux server), Keychain otherwise; injectable getter still wins
    import os
    import tempfile
    from us_market import (KEYCHAIN_KEY_ID, KEYCHAIN_SECRET,
                           _default_secret, creds)
    assert creds(lambda s: f"fake-{s}") == \
        (f"fake-{KEYCHAIN_KEY_ID}", f"fake-{KEYCHAIN_SECRET}")
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / KEYCHAIN_KEY_ID).write_text("kid-123\n")
        (Path(td) / KEYCHAIN_SECRET).write_text("sec-456\n")
        old = os.environ.get("CREDENTIALS_DIRECTORY")
        os.environ["CREDENTIALS_DIRECTORY"] = td
        try:
            assert creds() == ("kid-123", "sec-456")   # file backend, trimmed
            assert _default_secret(KEYCHAIN_KEY_ID) == "kid-123"
        finally:
            if old is None:
                del os.environ["CREDENTIALS_DIRECTORY"]
            else:
                os.environ["CREDENTIALS_DIRECTORY"] = old
    print("ok")


if __name__ == "__main__":
    check()
