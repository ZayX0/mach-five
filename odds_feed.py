"""Pinnacle moneylines from The Odds API (api.the-odds-api.com).

Base pattern lifted from mlb-prop-finder's data/sources/odds.py: GET with the
key in the `apiKey` query param. For moneylines we use the bulk /odds
endpoint (one billed call returns every game) instead of the events +
per-event calls that repo needs for player props.

Set ODDS_API_KEY in a .env file (loaded here via python-dotenv).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import requests
from dotenv import load_dotenv

load_dotenv()

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
SHARP_BOOK = "pinnacle"

Transport = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True)
class Game:
    """One Pinnacle moneyline. Side A = home, side B = away."""
    home: str
    away: str
    price_home: int   # American
    price_away: int
    commence_time: datetime | None  # first pitch, for Polymarket matching


def _redact_key(message: str) -> str:
    return re.sub(r"(apiKey=)[^&\s]+", r"\1***", message)


def requests_transport(url: str, params: dict[str, Any]) -> Any:
    r = requests.get(url, params=params, timeout=20)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:  # never leak the apiKey into logs
        raise requests.HTTPError(_redact_key(str(e)), response=e.response) from None
    return r.json()


def pinnacle_moneylines(
    *, api_key: str | None = None, get_json: Transport = requests_transport
) -> dict[str, Game]:
    """market_id -> Game (Pinnacle h2h only). Side A = home, side B = away.
    Games where Pinnacle has not posted a two-way h2h line are skipped.
    """
    key = api_key or os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("ODDS_API_KEY not set — put it in a .env file")

    events = get_json(
        f"{ODDS_API_BASE}/sports/{SPORT}/odds",
        {
            "apiKey": key,
            "bookmakers": SHARP_BOOK,   # bills only Pinnacle's region
            "markets": "h2h",
            "oddsFormat": "american",
        },
    )
    out: dict[str, Game] = {}
    for event in events:
        ext = _extract(event)
        if ext is None:
            continue
        market_id, game = ext
        if market_id in out:
            # Same matchup priced twice — a doubleheader, or the series'
            # next game posted while today's is still live. A plain
            # away@home key silently drops one (seen live 2026-08-05:
            # tomorrow's CWS-BOS REPLACED the in-session pilot game and
            # the loop held on a phantom stale anchor). First game keeps
            # the plain key (the API orders by commence time); later
            # duplicates get a commence-stamped key.
            stamp = (f"{game.commence_time:%Y%m%d%H%M}"
                     if game.commence_time else str(len(out)))
            market_id = f"{market_id}|{stamp}"
        out[market_id] = game
    return out


def _extract(event: dict) -> tuple[str, Game] | None:
    """Pull the (home, away) Pinnacle h2h prices for one event, or None."""
    home, away = event.get("home_team"), event.get("away_team")
    market_id = f"{away}@{home}"
    prices: dict[str, int] = {}
    for book in event.get("bookmakers", []):
        if book.get("key") != SHARP_BOOK:
            continue
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes", []):
                if o.get("name") and o.get("price") is not None:
                    prices[o["name"]] = int(o["price"])
    if home in prices and away in prices:
        game = Game(home, away, prices[home], prices[away], _parse_ts(event.get("commence_time")))
        return market_id, game
    return None


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
