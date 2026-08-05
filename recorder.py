"""Recorder — capture the raw series needed to time quoting, from the venue
we can actually trade: Polymarket US (see us_market.py; the international
exchange this repo's clob.py reads has a separate book).

Per game, appends one JSONL file under recordings/MM-DD-YYYY/ (one folder
per MLB slate day, Eastern date) with record types:

    {"type": "meta", ...}      once: teams, side tokens, US slug + long side,
                               venue, commence_ts
    {"type": "pinnacle", ...}  Pinnacle prices + devigged fv, per odds poll
    {"type": "book", ...}      A=home-frame book per side per market poll
                               (side A carries the raw state + stats too;
                               side B is the complement mirror — the US venue
                               has ONE instrument per game)
    {"type": "trade", ...}     markets-WebSocket prints, normalized so
                               taker_side is always SELL on the side a seller
                               crossed (see us_market.trade_line), deduped by
                               trade id, timestamped by the EXCHANGE clock
    {"type": "lineup", ...}    first observation of a side's CONFIRMED lineup
                               (MLB Stats API battingOrder, keyless) — ts is
                               when WE saw it, an upper bound on posting time
    {"type": "rescheduled"}    appended to a postponed game's file when its
                               makeup is picked up; `to` names the new file,
                               whose meta carries the matching reschedule_of

Billing: only the Pinnacle poll costs Odds API credits (1 request per poll),
so it runs two-speed — ODDS_POLL_SEC near first pitches, ODDS_POLL_SLOW_SEC
otherwise (see odds_interval) — to fit the 20k/month quota.
US books are keyless REST; the trade WebSocket needs the Keychain API creds
(read at startup, scrubbed from every log line). Files are append-only and
resume on restart (trade ids and lineup state are rebuilt by scanning).
Trades that print while the WS is down are lost; book `stats` snapshots
(cumulative sharesTraded/notionalTraded) bound what a gap missed.

Usage:  python3 recorder.py run     (needs ODDS_API_KEY; Ctrl-C to stop)
Offline check: python3 tests/check_recorder.py
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import lineups
import us_market
from fair_value import fair_prob
from odds_feed import Game, pinnacle_moneylines, _redact_key

REC_DIR = Path(__file__).parent / "recordings"

ODDS_POLL_SEC = 60.0        # fast cadence; 1 billed Odds API request per poll
ODDS_POLL_SLOW_SEC = 600.0  # slow cadence: no game near a first pitch
FAST_BEFORE_H = 1.5      # fast window opens this long before a first pitch
FAST_AFTER_H = 0.5       # ...and closes this long after (markout-horizon tail)
MARKET_POLL_SEC = 15.0   # US books, keyless
LINEUP_POLL_SEC = 120.0  # MLB Stats API boxscores, keyless
RECORD_FROM_H = 18.0     # US pre-game books are open (and trading) overnight
RECORD_UNTIL_H = 5.0     # observe what the US book does post-start
BOOK_DEPTH = 5           # levels per side kept in each book snapshot
RESOLVED_404S = 5        # consecutive book 404s = market gone, not a blip

_SECRETS: list[str] = []  # scrubbed from every log line


@dataclass
class Recording:
    """One game's open recording: identity + dedup/lineup state."""
    game: Game
    slug: str                             # US market slug (aec-mlb-...)
    long_side: str                        # 'A' home-long / 'B' away-long
    token_a: str
    token_b: str
    path: Path
    seen: set = field(default_factory=set)               # trade ids
    game_pk: int | None = None            # MLB Stats API id, lazily resolved
    lineup_seen: set[str] = field(default_factory=set)  # sides already logged
    book_404s: int = 0                    # consecutive 404 streak on book polls

    def append(self, obj: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(obj) + "\n")


def _slug(market_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", market_id.lower()).strip("-")


def in_window(game: Game, now: datetime) -> bool:
    """Record from RECORD_FROM_H before first pitch to RECORD_UNTIL_H after."""
    if game.commence_time is None:
        return False
    hours = (now - game.commence_time).total_seconds() / 3600.0
    return -RECORD_FROM_H <= hours <= RECORD_UNTIL_H


def odds_interval(games: list[Game], now: datetime) -> float:
    """Seconds until the next Pinnacle poll (two-speed, quota-bound: the
    account gets 20k billed calls/month; flat 60s polling burns ~43k).

    Fast (ODDS_POLL_SEC) while any known game is inside its critical window
    — FAST_BEFORE_H before first pitch to FAST_AFTER_H after — which is
    where fv resolution drives replay's markout tables. Slow otherwise,
    shortened so a sleep never overshoots a known game's window entry.
    ponytail: a game first appearing in the feed already inside its window
    (late makeup listing) is picked up one slow poll late; the slate is
    normally known 18h out via the recording window.
    """
    lo, hi = -FAST_BEFORE_H * 3600.0, FAST_AFTER_H * 3600.0
    next_entry = None
    for g in games:
        if g.commence_time is None:
            continue
        dt = (now - g.commence_time).total_seconds()
        if lo <= dt <= hi:
            return ODDS_POLL_SEC
        if dt < lo:  # window entry still ahead
            entry = lo - dt
            next_entry = entry if next_entry is None else min(next_entry, entry)
    if next_entry is None:
        return ODDS_POLL_SLOW_SEC
    return min(ODDS_POLL_SLOW_SEC, max(ODDS_POLL_SEC, next_entry))


def _prior_recording(rec_dir: Path, slug: str, commence_ts: float) -> Path | None:
    """Newest existing recording of the same US market (slug) with a
    DIFFERENT first pitch — i.e. this game is that one rescheduled. Ordinary
    series games never collide: each has its own market slug. Skips the
    recordings/intl archive (different venue, different meta shape)."""
    best = None
    for p in sorted(rec_dir.glob("*/*.jsonl")):
        if "intl" in p.parts:
            continue
        try:
            with p.open() as f:
                meta = json.loads(f.readline() or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        if (meta.get("type") == "meta" and meta.get("slug") == slug
                and meta.get("commence_ts") != commence_ts):
            best = p
    return best


def open_recording(
    market_id: str, game: Game, *, rec_dir: Path = REC_DIR,
    search_cache: dict | None = None, get_json=None,
) -> Recording | None:
    """Match the game to its US market and open (or resume) its file.
    None when no US moneyline covers this game.

    Files live in one folder per MLB slate day (Eastern date, MM-DD-YYYY —
    a late West Coast game is next-day UTC but belongs to the same slate).
    A rescheduled game keeps its old file in the original day's folder; the
    makeup game gets a fresh file whose meta carries `reschedule_of`, and
    the old file gets a `rescheduled` line pointing forward."""
    kw = {"search_cache": search_cache}
    if get_json is not None:
        kw["get_json"] = get_json
    market = us_market.find_market(game, **kw)
    if market is None:
        return None
    long = us_market.long_side(market, game.home)
    slug = market.get("slug")
    if long is None or not slug:
        return None
    day_dir = rec_dir / f"{lineups.mlb_day(game.commence_time):%m-%d-%Y}"
    day_dir.mkdir(parents=True, exist_ok=True)
    # commence datetime (not just date) in the name: consecutive-day games
    # share a UTC date and the same away@home id (e.g. a 9:10PM CT game and
    # the next day's 3:10PM game are both Aug 1 UTC)
    path = day_dir / f"{game.commence_time:%Y-%m-%d-%H%M}_{_slug(market_id)}.jsonl"
    rec = Recording(game, slug, long, f"{slug}#A", f"{slug}#B", path)
    if path.exists():  # resume: rebuild dedup/lineup state from disk
        for line in path.read_text().splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "trade":
                rec.seen.add(obj.get("id"))
            elif obj.get("type") == "lineup":
                rec.lineup_seen.add(obj.get("side"))
                rec.game_pk = obj.get("game_pk") or rec.game_pk
    else:
        meta = {
            "type": "meta", "market_id": market_id,
            "home": game.home, "away": game.away,
            "token_a": rec.token_a, "token_b": rec.token_b,
            "slug": slug, "long_side": long, "venue": "us",
            "commence_ts": game.commence_time.timestamp(),
        }
        prior = _prior_recording(rec_dir, slug, meta["commence_ts"])
        if prior is not None:  # link the two halves of a reschedule
            meta["reschedule_of"] = str(prior.relative_to(rec_dir))
            with prior.open("a") as f:
                f.write(json.dumps({
                    "type": "rescheduled", "ts": time.time(),
                    "to": str(path.relative_to(rec_dir))}) + "\n")
        rec.append(meta)
    return rec


def record_odds(rec: Recording, game: Game, ts: float) -> None:
    rec.append({
        "type": "pinnacle", "ts": ts,
        "price_home": game.price_home, "price_away": game.price_away,
        "fv": fair_prob(game.price_home, game.price_away),
    })


def record_books(rec: Recording, ts: float, get_json=None) -> None:
    """One US book fetch -> two A-frame side lines (B is the mirror)."""
    kw = {"get_json": get_json} if get_json is not None else {}
    raw = us_market.market_book(rec.slug, **kw)
    a = us_market.book_a_frame(raw, rec.long_side)
    rec.append({"type": "book", "ts": ts, "side": "A",
                "best_bid": a["best_bid"], "best_ask": a["best_ask"],
                "bids": a["bids"][:BOOK_DEPTH], "asks": a["asks"][:BOOK_DEPTH],
                "state": a.get("state"), "stats": a.get("stats")})
    b = us_market.mirror(a)
    rec.append({"type": "book", "ts": ts, "side": "B",
                "best_bid": b["best_bid"], "best_ask": b["best_ask"],
                "bids": b["bids"][:BOOK_DEPTH], "asks": b["asks"][:BOOK_DEPTH]})


def record_trade(rec: Recording, trade: dict) -> bool:
    """Normalize + dedup + append one WS trade; True when written."""
    line = us_market.trade_line(trade, rec.long_side, rec.token_a, rec.token_b)
    if line is None or (line.get("id") and line["id"] in rec.seen):
        return False
    if line.get("id"):
        rec.seen.add(line["id"])
    rec.append(line)
    return True


def record_lineups(rec: Recording, sched: list[dict], ts: float,
                   *, get_json=None) -> None:
    """Log each side's confirmed lineup the first time it's observed.
    No-op (and no HTTP) once both sides are logged."""
    if len(rec.lineup_seen) == 2:
        return
    if rec.game_pk is None:
        rec.game_pk = lineups.find_game_pk(rec.game, sched)
        if rec.game_pk is None:
            return  # not in the MLB schedule (yet); retried next poll
    kw = {"get_json": get_json} if get_json is not None else {}
    home, away = lineups.lineups_posted(rec.game_pk, **kw)
    for side, posted in (("A", home), ("B", away)):
        if posted and side not in rec.lineup_seen:
            rec.lineup_seen.add(side)
            rec.append({"type": "lineup", "ts": ts, "side": side,
                        "game_pk": rec.game_pk})


def is_resolved_404(rec: Recording, exc: Exception) -> bool:
    """Classify one failed market poll. A 404 means the US book is gone —
    normal once a market expires — but a lone 404 could be a blip, so only
    RESOLVED_404S consecutive ones confirm it (returns True: close the
    recording). Any other error resets the streak."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 404:
        rec.book_404s += 1
        return rec.book_404s >= RESOLVED_404S
    rec.book_404s = 0
    return False


def _log(msg: str) -> None:
    for s in _SECRETS:
        msg = msg.replace(s, "***")
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now}] {_redact_key(msg)}", flush=True)


def run() -> None:
    recs: dict[str, Recording] = {}
    by_slug: dict[str, Recording] = {}
    unmatched: set[str] = set()
    resolved: set[str] = set()
    games: dict[str, Game] = {}
    odds_due = last_lineups = 0.0
    cadence = ODDS_POLL_SEC

    def on_trade(trade: dict) -> None:
        rec = by_slug.get(str(trade.get("marketSlug")))
        if rec is not None:
            record_trade(rec, trade)

    feed = None
    try:
        key_id, secret = us_market.creds()
        _SECRETS.extend((key_id, secret))
        feed = us_market.TradeFeed(key_id, secret, on_trade, log=_log)
        feed.start()
    except Exception as e:
        _log(f"no US API creds ({type(e).__name__}); recording books only")

    while True:
        now_ts = time.time()
        now_dt = datetime.now(timezone.utc)

        if now_ts >= odds_due:
            poll_ok = True
            try:
                games = pinnacle_moneylines()
            except Exception as e:  # keep recording on feed hiccups
                _log(f"odds poll failed: {e}")
                games, poll_ok = {}, False
            for market_id, game in games.items():
                if not in_window(game, now_dt):
                    continue
                # away@home repeats across a series; commence time makes the
                # key unique per game (ponytail: when the odds feed lists two
                # games of one matchup at once, its away@home dict keeps only
                # one — that game's fv series just gets sparser)
                key = f"{market_id}|{game.commence_time:%Y%m%d%H%M}"
                if key in unmatched or key in resolved:
                    continue
                if key not in recs:
                    try:
                        rec = open_recording(market_id, game)
                    except Exception as e:
                        _log(f"match failed {market_id}: {e}")
                        continue
                    if rec is None:
                        _log(f"no US market for {market_id}")
                        unmatched.add(key)
                        continue
                    _log(f"recording {market_id} -> {rec.path.name} "
                         f"({rec.slug}, long={rec.long_side})")
                    recs[key] = rec
                    by_slug[rec.slug] = rec
                record_odds(recs[key], game, now_ts)
            if feed is not None:
                feed.watch(set(by_slug))
            # two-speed cadence: recs cover games the feed drops mid-window;
            # a failed poll retries fast regardless
            known = list(games.values()) + [r.game for r in recs.values()]
            interval = odds_interval(known, now_dt) if poll_ok else ODDS_POLL_SEC
            if interval != cadence:
                _log(f"odds cadence {cadence:.0f}s -> {interval:.0f}s")
                cadence = interval
            odds_due = now_ts + interval

        for key, rec in list(recs.items()):
            if not in_window(rec.game, now_dt):
                _log(f"window closed for {rec.slug}")
                del recs[key]
                by_slug.pop(rec.slug, None)
                continue
            try:
                record_books(rec, time.time())
                rec.book_404s = 0
            except Exception as e:
                if is_resolved_404(rec, e):
                    _log(f"market resolved for {rec.slug} "
                         f"(book 404 x{RESOLVED_404S}); closing recording")
                    resolved.add(key)
                    del recs[key]
                    by_slug.pop(rec.slug, None)
                elif rec.book_404s == 0:   # real error, not a quiet 404 streak
                    _log(f"market poll failed {rec.slug}: {e}")

        if now_ts - last_lineups >= LINEUP_POLL_SEC:
            last_lineups = now_ts
            pending = [r for r in recs.values() if len(r.lineup_seen) < 2]
            scheds: dict = {}
            for rec in pending:
                day = lineups.mlb_day(rec.game.commence_time)
                try:
                    if day not in scheds:
                        scheds[day] = lineups.schedule(day)
                    record_lineups(rec, scheds[day], time.time())
                    if len(rec.lineup_seen) == 2:
                        _log(f"both lineups posted for "
                             f"{rec.game.away}@{rec.game.home}")
                except Exception as e:
                    _log(f"lineup poll failed {rec.game.home}: {e}")

        time.sleep(MARKET_POLL_SEC)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        run()
    else:
        print("usage: python3 recorder.py run   "
              "(offline check: python3 tests/check_recorder.py)")
