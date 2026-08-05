"""Offline self-check for lineups.py — canned payloads, no network."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineups import find_game_pk, lineups_posted, mlb_day, schedule
from odds_feed import Game


def check() -> None:
    # Eastern game day: a 02:40 UTC Aug 1 start is a July 31 MLB day
    assert mlb_day(datetime(2026, 7, 31, 17, 10, tzinfo=timezone.utc)) == date(2026, 7, 31)
    assert mlb_day(datetime(2026, 8, 1, 2, 40, tzinfo=timezone.utc)) == date(2026, 7, 31)

    sched_payload = {"dates": [{"date": "2026-07-29", "games": [
        {"gamePk": 111, "gameDate": "2026-07-29T17:10:00Z",
         "teams": {"home": {"team": {"name": "Chicago Cubs"}},
                   "away": {"team": {"name": "New York Yankees"}}}},
        {"gamePk": 222, "gameDate": "2026-07-29T23:05:00Z",  # doubleheader G2
         "teams": {"home": {"team": {"name": "Chicago Cubs"}},
                   "away": {"team": {"name": "New York Yankees"}}}},
        {"gamePk": 333, "gameDate": "2026-07-29T18:10:00Z",
         "teams": {"home": {"team": {"name": "Boston Red Sox"}},
                   "away": {"team": {"name": "Toronto Blue Jays"}}}},
    ]}]}
    sched = schedule(date(2026, 7, 29), get_json=lambda url, **p: sched_payload)
    assert [g["gamePk"] for g in sched] == [111, 222, 333]

    dt = datetime(2026, 7, 29, 17, 10, tzinfo=timezone.utc)
    game = Game("Chicago Cubs", "New York Yankees", -136, 124, dt)
    assert find_game_pk(game, sched) == 111  # G1, not the 23:05 G2
    g2 = Game("Chicago Cubs", "New York Yankees", -136, 124,
              datetime(2026, 7, 29, 23, 5, tzinfo=timezone.utc))
    assert find_game_pk(g2, sched) == 222
    nomatch = Game("A Team", "B Team", 100, -100, dt)
    assert find_game_pk(nomatch, sched) is None
    assert find_game_pk(Game("Chicago Cubs", "New York Yankees", -136, 124,
                             None), sched) is None

    box = {"teams": {"home": {"battingOrder": [665742, 592450]},
                     "away": {"battingOrder": []}}}
    assert lineups_posted(111, get_json=lambda url, **p: box) == (True, False)
    assert lineups_posted(111, get_json=lambda url, **p: {}) == (False, False)
    print("ok")


if __name__ == "__main__":
    check()
