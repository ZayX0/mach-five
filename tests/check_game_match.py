"""Offline self-check for game_match.py — pre-seeded caches, no network."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game_match import (_is_team, _nicknames, find_market, find_tokens,
                        tokens_from_market)
from odds_feed import Game


def check() -> None:
    assert _nicknames("Boston Red Sox") == ["red sox", "sox"]
    assert _is_team("Blue Jays", "Toronto Blue Jays") and not _is_team("Mets", "New York Yankees")

    dt = datetime(2026, 7, 29, 17, 10, tzinfo=timezone.utc)
    game = Game("Chicago Cubs", "New York Yankees", -136, 124, dt)

    # Two title matches on the same day (doubleheader): only the one whose
    # resolution-text start is near game_dt should win.
    ev_g1 = {"id": 1, "closed": False, "title": "Yankees vs. Cubs",
             "slug": "mlb-nyy-chc-2026-07-29"}
    ev_g2 = {"id": 2, "closed": False, "title": "Yankees vs. Cubs",
             "slug": "mlb-nyy-chc-2026-07-29"}
    search_cache = {"Chicago Cubs": [ev_g1, ev_g2], "New York Yankees": []}
    markets_cache = {
        1: [{"description": "...scheduled for July 29 at 1:10PM ET: ...",
             "closed": False, "question": "Cubs vs Yankees",
             "outcomes": '["Chicago Cubs","New York Yankees"]',
             "clobTokenIds": '["tokA","tokB"]', "volumeNum": 5000,
             "conditionId": "0xcond1"}],
        2: [{"description": "...scheduled for July 29 at 7:05PM ET: ...",
             "outcomes": '["Chicago Cubs","New York Yankees"]',
             "clobTokenIds": '["x","y"]'}],
    }
    got = find_tokens(game, search_cache=search_cache, markets_cache=markets_cache)
    assert got == ("tokA", "tokB"), got  # A=home Cubs, B=away Yankees, game 1

    # find_market surfaces the full dict (recorder needs conditionId)
    market = find_market(game, search_cache=search_cache, markets_cache=markets_cache)
    assert market is not None and market["conditionId"] == "0xcond1", market
    assert tokens_from_market(market, game) == ("tokA", "tokB")

    # No team match -> None
    empty = find_tokens(Game("A Team", "B Team", 100, -100, dt),
                        search_cache={"A Team": [], "B Team": []}, markets_cache={})
    assert empty is None
    print("ok")


if __name__ == "__main__":
    check()
