"""Confirmed-lineup detection from the keyless MLB Stats API.

The news guard's trigger signal: a game's boxscore carries a battingOrder
only once that lineup is CONFIRMED (typically posted 2-4h before first
pitch). Ported from mlb-prop-finder's data/sources/lineups.py + mlb_api.py,
cut down to the one question mach-five asks: are this game's lineups posted
yet? Absence is the signal — no battingOrder means not posted, never
projected (that repo's AGENTS.md rule).

recorder.py logs the first observation of each side's posted lineup as a
`lineup` event line; replay.py then measures fills and markout relative to
lineup posting instead of clock time. The same signal is what a live
toxic-flow guard would key on (pull/widen quotes when a lineup just posted
and Pinnacle hasn't repriced yet).
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import clob
from game_match import _is_team, _parse_ts

STATS_API = "https://statsapi.mlb.com/api/v1"
MATCH_TOLERANCE_S = 2 * 3600  # doubleheader disambiguation, like game_match


def mlb_day(commence: datetime) -> date:
    """The US game day the MLB schedule endpoint keys on (Eastern date —
    a late West Coast game is next-day in UTC but same-day Eastern)."""
    return commence.astimezone(ZoneInfo("America/New_York")).date()


def schedule(day: date, *, get_json=clob._get) -> list[dict]:
    """Raw schedule games for one MLB day (gamePk, teams, gameDate, status)."""
    data = get_json(f"{STATS_API}/schedule", sportId=1, date=day.isoformat())
    return [g for d in data.get("dates", []) for g in d.get("games", [])]


def find_game_pk(game, sched: list[dict]) -> int | None:
    """gamePk matching an odds_feed.Game: both teams match (exact-or-nickname,
    reusing game_match._is_team) and start time within tolerance — nearest
    start wins, which keeps doubleheaders apart."""
    best: tuple[float, int] | None = None
    for g in sched:
        teams = g.get("teams", {})
        home = teams.get("home", {}).get("team", {}).get("name", "")
        away = teams.get("away", {}).get("team", {}).get("name", "")
        if not (_is_team(home, game.home) and _is_team(away, game.away)):
            continue
        gd = _parse_ts(g.get("gameDate"))
        if gd is None or game.commence_time is None:
            continue
        dist = abs((gd - game.commence_time).total_seconds())
        if dist <= MATCH_TOLERANCE_S and (best is None or dist < best[0]):
            best = (dist, g.get("gamePk"))
    return best[1] if best else None


def lineups_posted(game_pk: int, *, get_json=clob._get) -> tuple[bool, bool]:
    """(home_posted, away_posted): does the boxscore carry a battingOrder?"""
    box = get_json(f"{STATS_API}/game/{game_pk}/boxscore")
    teams = box.get("teams", {})

    def posted(side: str) -> bool:
        return bool(teams.get(side, {}).get("battingOrder"))

    return posted("home"), posted("away")
