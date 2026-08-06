"""Offline self-check for odds_feed.py — canned Odds API payload, no key."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds_feed import (BOOKS, COMPARE_BOOK, moneylines_by_book,
                       pinnacle_moneylines, _redact_key)


def check() -> None:
    payload = [
        {
            "home_team": "Chicago Cubs",
            "away_team": "New York Yankees",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Chicago Cubs", "price": -136},
                                {"name": "New York Yankees", "price": 124},
                            ],
                        }
                    ],
                },
                {
                    "key": "betonlineag",  # compare book, same event
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Chicago Cubs", "price": -140},
                                {"name": "New York Yankees", "price": 126},
                            ],
                        }
                    ],
                },
                {"key": "fanduel", "markets": []},  # ignored: not requested
            ],
        },
        {"home_team": "A", "away_team": "B", "bookmakers": []},  # skipped: no line
    ]
    payload[0]["commence_time"] = "2026-07-30T17:10:00Z"
    seen_params: dict = {}

    def transport(url, params):
        seen_params.update(params)
        return payload

    out = pinnacle_moneylines(api_key="x", get_json=transport)
    assert set(out) == {"New York Yankees@Chicago Cubs"}, out
    g = out["New York Yankees@Chicago Cubs"]
    assert (g.home, g.away, g.price_home, g.price_away) == (
        "Chicago Cubs", "New York Yankees", -136, 124), g
    assert g.commence_time is not None and g.commence_time.hour == 17
    # both books ride in ONE call (<= 10 bookmakers bills as one region)
    assert seen_params["bookmakers"] == ",".join(BOOKS) == \
        "pinnacle,betonlineag", seen_params

    # the by-book view: compare book parsed from the same payload, absent
    # where it has no line, and never leaking into the pinnacle dict
    by_book = moneylines_by_book(api_key="x", get_json=transport)
    assert set(by_book) == set(BOOKS)
    bo = by_book[COMPARE_BOOK]["New York Yankees@Chicago Cubs"]
    assert (bo.price_home, bo.price_away) == (-140, 126), bo
    assert bo.commence_time == g.commence_time
    only_pinnacle = [dict(payload[0], bookmakers=payload[0]["bookmakers"][:1])]
    by_book2 = moneylines_by_book(api_key="x",
                                  get_json=lambda url, params: only_pinnacle)
    assert by_book2[COMPARE_BOOK] == {} and len(by_book2["pinnacle"]) == 1

    # same matchup twice (doubleheader / next series game posted early):
    # BOTH must survive — first keeps the plain key, later ones get a
    # commence-stamped key (2026-08-05: tomorrow's game silently replaced
    # the live pilot game)
    import copy
    game2 = copy.deepcopy(payload[0])
    game2["commence_time"] = "2026-07-31T17:10:00Z"
    out = pinnacle_moneylines(api_key="x",
                              get_json=lambda url, params: [payload[0], game2])
    assert set(out) == {"New York Yankees@Chicago Cubs",
                        "New York Yankees@Chicago Cubs|202607311710"}, out
    assert out["New York Yankees@Chicago Cubs"].commence_time.day == 30
    assert out["New York Yankees@Chicago Cubs|202607311710"].commence_time.day == 31

    assert _redact_key("boom apiKey=secret123&x=1") == "boom apiKey=***&x=1"
    print("ok")


if __name__ == "__main__":
    check()
