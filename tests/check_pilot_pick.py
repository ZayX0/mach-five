"""Offline self-check for pilot_pick.py — injected slate, no network."""
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_pick import MAX_AHEAD_SEC, MIN_LEAD_SEC, notional_cents, rank


@dataclass
class G:
    home: str
    commence_time: datetime | None


def dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def check() -> None:
    now = 1_785_952_800.0
    games = [
        G("started", dt(now - 600)),              # already underway
        G("too-soon", dt(now + MIN_LEAD_SEC / 2)),  # inside the lead buffer
        G("evening1", dt(now + 5 * 3600)),
        G("evening2", dt(now + 5 * 3600)),
        G("evening3", dt(now + 7 * 3600)),
        G("nomatch", dt(now + 6 * 3600)),         # no US market found
        G("tomorrow", dt(now + MAX_AHEAD_SEC + 60)),  # next slate
        G("tbd", None),                           # no commence time
    ]
    notional = {"evening1": 40_000_00, "evening2": 385_000_00,
                "evening3": 385_000_00}

    def market_for(g):
        return None if g.home == "nomatch" else {"slug": g.home}

    rows = rank(games, now, market_for, lambda s: notional.get(s, 0))
    # only in-window, matched games survive
    assert [r[0] for r in rows] == ["evening2", "evening3", "evening1"], rows
    # highest notional wins; the tie broke to the earlier pitch
    assert rows[0][0] == "evening2" and rows[0][2] == 385_000_00
    assert rows[0][1] == now + 5 * 3600

    # empty slate / nothing eligible -> empty ranking (launcher aborts)
    assert rank([], now, market_for, lambda s: 0) == []
    assert rank([G("started", dt(now - 600))], now, market_for,
                lambda s: 0) == []

    # a game with no tape yet still ranks (last), it isn't dropped
    rows = rank([G("evening1", dt(now + 5 * 3600)),
                 G("quiet", dt(now + 5 * 3600))],
                now, market_for, lambda s: notional.get(s, 0))
    assert [r[0] for r in rows] == ["evening1", "quiet"], rows
    # notional_cents: the gateway's money-dict shape (the 2026-08-05 bug —
    # a bare int() on the dict raised and every game ranked $0)
    assert notional_cents(
        {"notionalTraded": {"value": "842289.7100", "currency": "USD"}}
    ) == 842289
    assert notional_cents({"notionalTraded": "1234.5"}) == 1234
    assert notional_cents({"notionalTraded": None}) == 0
    assert notional_cents({}) == 0
    assert notional_cents(None) == 0
    assert notional_cents({"notionalTraded": {"currency": "USD"}}) == 0
    assert notional_cents({"notionalTraded": "garbage"}) == 0
    print("ok")


if __name__ == "__main__":
    check()
