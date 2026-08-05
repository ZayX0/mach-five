"""Offline self-check for recorder.py — canned payloads, tmp dir, no
network, no threads."""
import contextlib
import io
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import us_market
from odds_feed import Game
from recorder import (ODDS_POLL_SEC, ODDS_POLL_SLOW_SEC, RESOLVED_404S,
                      _SECRETS, _log, _slug, in_window, is_resolved_404,
                      odds_interval, open_recording, record_books,
                      record_lineups, record_odds, record_trade)


def check() -> None:
    assert _slug("New York Yankees@Chicago Cubs") == "new-york-yankees-chicago-cubs"

    # two-speed odds cadence: fast inside the critical window (1.5h before
    # pitch to 0.5h after), slow otherwise but never past a window entry
    pitch = datetime(2026, 8, 1, 23, 0, tzinfo=timezone.utc)
    g = Game("Chicago Cubs", "New York Yankees", -136, 124, pitch)
    def at(h: float) -> datetime:
        return datetime.fromtimestamp(pitch.timestamp() + h * 3600, timezone.utc)
    assert odds_interval([g], at(-1.0)) == ODDS_POLL_SEC        # inside window
    assert odds_interval([g], at(-1.5)) == ODDS_POLL_SEC        # window edge
    assert odds_interval([g], at(0.4)) == ODDS_POLL_SEC         # just after pitch
    assert odds_interval([g], at(1.0)) == ODDS_POLL_SLOW_SEC    # window passed
    assert odds_interval([g], at(-8.0)) == ODDS_POLL_SLOW_SEC   # far out
    assert odds_interval([g], at(-1.6)) == 360.0                # clamp to entry
    assert odds_interval([g], at(-1.51)) == ODDS_POLL_SEC       # entry < fast
    assert odds_interval([], at(0.0)) == ODDS_POLL_SLOW_SEC     # empty slate
    assert odds_interval([Game("A", "B", -110, -110, None)], at(0.0)) \
        == ODDS_POLL_SLOW_SEC                                   # no commence time
    two = [g, Game("A", "B", -110, -110, at(6.0))]              # nearest game wins
    assert odds_interval(two, at(-1.0)) == ODDS_POLL_SEC

    dt = datetime(2026, 8, 1, 23, 15, tzinfo=timezone.utc)
    game = Game("Chicago Cubs", "New York Yankees", -136, 124, dt)
    assert in_window(game, datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc))
    assert not in_window(game, datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc))
    assert in_window(game, datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc))
    assert not in_window(game, datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc))
    assert not in_window(Game("A", "B", -110, -110, None), dt)

    ev = {"id": "1", "closed": False,
          "title": "New York Yankees vs Chicago Cubs",
          "startTime": "2026-08-01T23:15:00Z",
          "markets": [{"slug": "aec-mlb-nyy-chc-2026-08-01",
                       "sportsMarketType": us_market.WINNER_TYPE,
                       "closed": False}]}
    detail = {"slug": "aec-mlb-nyy-chc-2026-08-01",
              "marketSides": [
                  {"long": True, "team": {"name": "New York Yankees",
                                          "ordering": "away"}},
                  {"long": False, "team": {"name": "Chicago Cubs",
                                           "ordering": "home"}}]}
    book_payload = {"marketData": {
        "state": "OPEN", "stats": {"sharesTraded": "10"},
        "bids": [{"px": {"value": "0.40"}, "qty": "100"}],
        "offers": [{"px": {"value": "0.42"}, "qty": "80"}]}}

    def fake_get(url, **params):
        if url.endswith("/v1/search"):
            return {"events": [ev]}
        if url.endswith("/v1/markets"):
            return {"markets": [detail]}
        if url.endswith("/book"):
            return book_payload
        raise AssertionError(url)

    with tempfile.TemporaryDirectory() as tmp:
        rec = open_recording("New York Yankees@Chicago Cubs", game,
                             rec_dir=Path(tmp), get_json=fake_get)
        assert rec is not None and rec.long_side == "B", rec
        assert rec.token_a == "aec-mlb-nyy-chc-2026-08-01#A"
        # one folder per MLB slate day (Eastern date): 23:15Z Aug 1 is a
        # 7:15PM EDT game -> the 08-01-2026 slate
        assert rec.path.parent.name == "08-01-2026", rec.path
        record_odds(rec, game, 1000.0)
        record_books(rec, 1010.0, get_json=fake_get)

        # trades: instrument is Yankees(away)-long; a taker SELL prints on B,
        # a taker BUY prints on A at the complement; duplicate ids dropped
        t1 = {"marketSlug": rec.slug, "price": {"value": "0.405"},
              "quantity": {"value": "8.2"}, "id": "T1",
              "tradeTime": "2026-08-01T04:22:43.94Z",
              "taker": {"side": "ORDER_SIDE_SELL"}}
        t2 = dict(t1, id="T2", taker={"side": "ORDER_SIDE_BUY"})
        assert record_trade(rec, t1) and record_trade(rec, t2)
        assert not record_trade(rec, t1)       # dedup by id

        sched = [{"gamePk": 111, "gameDate": "2026-08-01T23:15:00Z",
                  "teams": {"home": {"team": {"name": "Chicago Cubs"}},
                            "away": {"team": {"name": "New York Yankees"}}}}]
        box = {"teams": {"home": {"battingOrder": [1]},
                         "away": {"battingOrder": []}}}
        record_lineups(rec, sched, 2000.0, get_json=lambda url, **p: box)
        assert rec.lineup_seen == {"A"} and rec.game_pk == 111

        # resume rebuilds trade ids + lineup state, writes no second meta
        rec2 = open_recording("New York Yankees@Chicago Cubs", game,
                              rec_dir=Path(tmp), get_json=fake_get)
        assert rec2.seen == {"T1", "T2"} and rec2.lineup_seen == {"A"}
        assert not record_trade(rec2, t2)

        lines = [json.loads(l) for l in rec.path.read_text().splitlines()]
        types = [l["type"] for l in lines]
        assert types == ["meta", "pinnacle", "book", "book",
                         "trade", "trade", "lineup"], types
        meta, _, book_a, book_b, tr1, tr2, _ = lines
        assert meta["venue"] == "us" and meta["long_side"] == "B"
        # instrument book was Yankees-long 0.40/0.42 -> A(home Cubs) 0.58/0.60
        assert (book_a["best_bid"], book_a["best_ask"]) == (0.58, 0.6), book_a
        assert book_a["state"] == "OPEN" and book_b["side"] == "B"
        assert (book_b["best_bid"], book_b["best_ask"]) == (0.4, 0.42)
        assert tr1["token"].endswith("#B") and tr1["price"] == 0.405
        assert tr2["token"].endswith("#A") and abs(tr2["price"] - 0.595) < 1e-9
        assert tr1["taker_side"] == tr2["taker_side"] == "SELL"

        # a late West Coast game (02:00Z = 10PM EDT the day before) files
        # under the previous day's slate folder
        west = Game("Athletics", "Detroit Tigers", -110, -110,
                    datetime(2026, 8, 2, 2, 0, tzinfo=timezone.utc))
        ev_w = {"id": "9", "closed": False, "title": "Detroit Tigers vs Athletics",
                "startTime": "2026-08-02T02:00:00Z",
                "markets": [{"slug": "aec-mlb-det-ath-2026-08-01",
                             "sportsMarketType": us_market.WINNER_TYPE,
                             "closed": False}]}
        det_w = {"slug": "aec-mlb-det-ath-2026-08-01",
                 "marketSides": [{"long": True, "team": {
                     "name": "Athletics", "ordering": "home"}}]}

        def fake_get_w(url, **params):
            if url.endswith("/v1/search"):
                return {"events": [ev_w]}
            if url.endswith("/v1/markets"):
                return {"markets": [det_w]}
            raise AssertionError(url)

        rec_w = open_recording("Detroit Tigers@Athletics", west,
                               rec_dir=Path(tmp), get_json=fake_get_w)
        assert rec_w.path.parent.name == "08-01-2026", rec_w.path

        # reschedule: the event moves to a new first pitch but the market
        # keeps its slug -> new file in the new day's folder, linked both
        # ways to the original
        makeup = Game("Athletics", "Detroit Tigers", -110, -110,
                      datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc))
        ev_m = dict(ev_w, startTime="2026-08-03T20:00:00Z")

        def fake_get_m(url, **params):
            if url.endswith("/v1/search"):
                return {"events": [ev_m]}
            if url.endswith("/v1/markets"):
                return {"markets": [det_w]}
            raise AssertionError(url)

        rec_m = open_recording("Detroit Tigers@Athletics", makeup,
                               rec_dir=Path(tmp), get_json=fake_get_m)
        assert rec_m.path.parent.name == "08-03-2026", rec_m.path
        meta_m = json.loads(rec_m.path.read_text().splitlines()[0])
        assert meta_m["reschedule_of"] == str(
            rec_w.path.relative_to(Path(tmp))), meta_m
        back = json.loads(rec_w.path.read_text().splitlines()[-1])
        assert back["type"] == "rescheduled" and back["to"] == str(
            rec_m.path.relative_to(Path(tmp))), back
        # the original (different slug, same slate) was NOT flagged
        first_meta = json.loads(rec.path.read_text().splitlines()[0])
        assert "reschedule_of" not in first_meta

        # resolved detection: 5 consecutive 404s confirm; anything else resets
        e404 = requests.HTTPError(response=SimpleNamespace(status_code=404))
        e500 = requests.HTTPError(response=SimpleNamespace(status_code=500))
        for i in range(RESOLVED_404S - 1):
            assert not is_resolved_404(rec, e404), i
        assert not is_resolved_404(rec, e500)       # non-404 resets the streak
        assert rec.book_404s == 0
        for i in range(RESOLVED_404S - 1):
            assert not is_resolved_404(rec, e404)
        assert is_resolved_404(rec, e404)           # 5th consecutive: resolved

    # secrets never reach the log
    _SECRETS.append("sekret123")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _log("boom sekret123 apiKey=abc123&x=1")
    out = buf.getvalue()
    assert "sekret123" not in out and "apiKey=***" in out, out
    _SECRETS.clear()
    print("ok")


if __name__ == "__main__":
    check()
