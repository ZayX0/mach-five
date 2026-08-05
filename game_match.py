"""Match a Pinnacle game to its Polymarket event + moneyline YES tokens.

Ported from mlb-prop-finder/scripts/probe_exchanges.py (match_event /
pick_moneyline). The hard part is doubleheaders: a makeup game's event keeps
the ORIGINAL date in its slug, so the covered-game start parsed from a
market's resolution text ("scheduled for July 29 at 1:10PM ET") outranks the
slug date. Falls back to slug-date, then nearest startDate, when no time is
parseable.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import clob

GAMMA = clob.GAMMA
SLUG_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")
NON_MONEYLINE_MARKERS = ("spread", "o/u", "1st 5", "inning", "run scored", "(")
SCHED_DESC = re.compile(
    r"scheduled for ([A-Za-z]+ \d{1,2})(?:st|nd|rd|th)? at "
    r"(\d{1,2}):(\d{2})\s?(AM|PM)\s*ET",
    re.IGNORECASE,
)
EVENT_TIME_TOLERANCE = 2 * 3600  # doubleheader disambiguation, seconds


def _norm(name: str) -> str:
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    )
    return " ".join(folded.lower().split())


def _nicknames(team_name: str) -> list[str]:
    """"Boston Red Sox" -> ["red sox", "sox"]. Both teams must match a title,
    so a generic suffix alone can't cross-match the wrong game."""
    parts = _norm(team_name).split()
    out = [" ".join(parts[-2:])] if len(parts) >= 2 else []
    out.append(parts[-1])
    return out


def _is_team(outcome: str, team_name: str) -> bool:
    o = _norm(outcome)
    return o == _norm(team_name) or o in _nicknames(team_name)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _slug_day(event: dict) -> date | None:
    m = SLUG_DATE.search(str(event.get("slug", "")))
    return date.fromisoformat(m.group(1)) if m else None


# --- Gamma discovery (cached; caches also make the self-check offline) -----
def search_events(team: str, cache: dict[str, list[dict]]) -> list[dict]:
    if team not in cache:
        data = clob._get(f"{GAMMA}/public-search", q=team)
        cache[team] = data.get("events", []) if isinstance(data, dict) else []
    return cache[team]


def event_markets(event: dict, cache: dict[Any, list[dict]]) -> list[dict]:
    ev_id = event.get("id")
    if ev_id is None:
        return []
    if ev_id not in cache:
        data = clob._get(f"{GAMMA}/events/{ev_id}")
        cache[ev_id] = data.get("markets", []) if isinstance(data, dict) else []
    return cache[ev_id]


def event_game_dt(event: dict, year: int, markets_cache: dict) -> datetime | None:
    """UTC start of the game this event COVERS, from market resolution text."""
    for m in event_markets(event, markets_cache):
        hit = SCHED_DESC.search(str(m.get("description", "")))
        if not hit:
            continue
        try:
            naive = datetime.strptime(
                f"{hit.group(1)} {year} {hit.group(2)}:{hit.group(3)} {hit.group(4).upper()}",
                "%B %d %Y %I:%M %p",
            )
            return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        except (ValueError, KeyError):
            continue
    return None


def match_event(
    home: str, away: str, game_dt: datetime | None,
    search_cache: dict, markets_cache: dict,
) -> dict | None:
    """Best open Polymarket event covering this game, or None."""
    candidates: dict[Any, dict] = {}
    for team in (home, away):
        for ev in search_events(team, search_cache):
            if not ev.get("closed"):
                candidates[ev.get("id") or ev.get("slug") or ev.get("title")] = ev

    def title_matches(ev: dict) -> bool:
        title = _norm(str(ev.get("title", "")))
        return any(n in title for n in _nicknames(home)) and any(
            n in title for n in _nicknames(away)
        )

    matched = [ev for ev in candidates.values() if title_matches(ev)]
    if not matched:
        return None

    year = game_dt.year if game_dt else datetime.now(timezone.utc).year
    # Primary: covered-game start beats slug date (doubleheaders/makeups).
    timed = []
    for ev in matched:
        ev_dt = event_game_dt(ev, year, markets_cache)
        if ev_dt is not None and game_dt is not None:
            timed.append((abs((ev_dt - game_dt).total_seconds()), ev))
    if timed:
        dist, ev = min(timed, key=lambda t: t[0])
        return ev if dist <= EVENT_TIME_TOLERANCE else None

    # No parseable time: slug-date exact, else nearest startDate.
    if game_dt is not None:
        exact = [ev for ev in matched if _slug_day(ev) == game_dt.date()]
        if exact:
            return exact[0]

    def distance(ev: dict) -> float:
        start = _parse_ts(ev.get("startDate"))
        return abs((start - game_dt).total_seconds()) if game_dt and start else 0.0

    return min(matched, key=distance)


def pick_moneyline(markets: list[dict], home: str, away: str) -> dict | None:
    """The moneyline market: exactly the two teams as outcomes, no
    spread/total/inning markers. Ties broken by volume (the liquid one)."""
    candidates = []
    for m in markets:
        if m.get("closed"):
            continue
        outcomes = clob._jsonish(m.get("outcomes")) or []
        if len(outcomes) != 2:
            continue
        if not (any(_is_team(str(o), home) for o in outcomes)
                and any(_is_team(str(o), away) for o in outcomes)):
            continue
        if any(marker in _norm(str(m.get("question", ""))) for marker in NON_MONEYLINE_MARKERS):
            continue
        candidates.append(m)
    if not candidates:
        return None
    return max(candidates, key=lambda m: float(m.get("volumeNum") or 0))


def find_market(
    game, *, search_cache: dict | None = None, markets_cache: dict | None = None
) -> dict | None:
    """The full Gamma moneyline market dict for this game, or None.

    `game` is an odds_feed.Game. The dict carries everything downstream code
    needs: `clobTokenIds`/`outcomes` (see tokens_from_market) and
    `conditionId` (the trade-tape key on data-api.polymarket.com).
    """
    search_cache = {} if search_cache is None else search_cache
    markets_cache = {} if markets_cache is None else markets_cache
    event = match_event(game.home, game.away, game.commence_time, search_cache, markets_cache)
    if event is None:
        return None
    return pick_moneyline(event_markets(event, markets_cache), game.home, game.away)


def tokens_from_market(market: dict, game) -> tuple[str, str] | None:
    """(token_a, token_b) YES clobTokenIds for (home, away), or None.

    Assigns each outcome's token to a side by team-name match, so token order
    in the market dict doesn't matter.
    """
    outcomes = clob._jsonish(market.get("outcomes")) or []
    tokens = clob._jsonish(market.get("clobTokenIds")) or []
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    by_side: dict[str, str] = {}
    for outcome, token in zip(outcomes, tokens):
        if _is_team(str(outcome), game.home):
            by_side["A"] = str(token)
        elif _is_team(str(outcome), game.away):
            by_side["B"] = str(token)
    if "A" in by_side and "B" in by_side:
        return by_side["A"], by_side["B"]
    return None


def find_tokens(
    game, *, search_cache: dict | None = None, markets_cache: dict | None = None
) -> tuple[str, str] | None:
    """(token_a, token_b) for (home, away), or None. Thin wrapper over
    find_market for callers that don't need the market dict itself."""
    market = find_market(game, search_cache=search_cache, markets_cache=markets_cache)
    if market is None:
        return None
    return tokens_from_market(market, game)
