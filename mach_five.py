"""Mach Five — Polymarket US market-maker loop.

Each game is one binary market with complementary sides, A = home YES /
B = away YES, price(A) + price(B) == 1.00. We only ever BUY YES on a side;
"selling A" == "buying B". Profit = assemble a complete set for < $1,
redeem for $1.

Venue: Polymarket US (us_market.py reads, us_orders.py writes — post-only
limit orders, prices in the venue's long-side terms handled by place_bid).
The quoting/skew math is venue-agnostic and exercised offline by replay.py.

WHEN to quote is guard.py's job (lineup gate + toxic-flow guard, from the
replay verdict): quotes rest only after both lineups are confirmed and
Pinnacle has digested them, and they are pulled on tape bursts, fv-vs-mid
divergence, trade-feed silence, game delay, or approaching first pitch.
Tape bursts pull IMMEDIATELY from the trade-WS callback thread — the guard
would be decorative if it waited for the next 60s loop tick.

SAFETY GATE: run() places REAL orders with REAL dollars. It refuses to
start unless MACH_FIVE_LIVE=1 is set. First session must be pilot-sized:
BASE_SIZE=100 AND MAX_INVENTORY=250 (rescale together or the skew is
decorative), one or two games. ponytail: inventory left at the stop is NOT
flattened automatically — close_position by hand or hold through
settlement; decide per NEXT_STEPS step 8.
"""
from __future__ import annotations
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import lineups
import us_market
import us_orders
from fair_value import fair_prob
from guard import GameGuard, TapeStats
from odds_feed import Game, pinnacle_moneylines

# ---- config ---------------------------------------------------------------
HALF_SPREAD = 0.006        # 0.6c each side of fair value
# PILOT SIZING (2026-08-04, $250 funding): worst case = 2*BASE resting +
# MAX_INVENTORY held = $180, $70 buffer. BASE:MAX must stay 2:5 or the skew
# is decorative. Research sizing (2000/5000) is pinned by replay.py so the
# paper tables keep their scale — rescale BOTH places deliberately.
BASE_SIZE = 40.0           # $ per side when flat
MAX_INVENTORY = 100.0      # $ of one-sided exposure before we stop adding
SKEW_STRENGTH = 0.004      # how hard inventory pushes quotes (in price units)
LOOP_SEC = 60.0            # requote cadence; every loop = 1 billed odds call
HORIZON_H = 8.0            # only track games with first pitch this close
LINEUP_POLL_SEC = 120.0    # MLB Stats API cadence for pending lineups


# ---- fair value -----------------------------------------------------------
def fair_value(price_a: int, price_b: int) -> float:
    """True prob that side A wins, in (0,1): devig the sharp quote."""
    return fair_prob(price_a, price_b)


# ---- the real logic: quote + skew ----------------------------------------
def quotes(fv: float, inventory: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return ((bid_A_price, bid_A_size), (bid_B_price, bid_B_size)).

    bid_A  = buy A YES   (want more A when we're short A / long B)
    bid_B  = buy B YES, quoted in A-frame as (1 - price)

    Inventory skew: long A (inventory>0) pushes both quotes DOWN so we buy
    less A and more B, driving us back toward flat.
    """
    skew = SKEW_STRENGTH * (inventory / MAX_INVENTORY)   # >0 when long A
    bid_a = fv - HALF_SPREAD - skew
    bid_b_in_a = fv + HALF_SPREAD - skew   # = offer A; buying B @ (1 - this)

    # shrink the side we're already loaded on, grow the side that flattens us
    load = max(-1.0, min(1.0, inventory / MAX_INVENTORY))
    size_a = BASE_SIZE * (1.0 - load)   # long A -> buy less A
    size_b = BASE_SIZE * (1.0 + load)   # long A -> buy more B
    return (bid_a, max(0.0, size_a)), (bid_b_in_a, max(0.0, size_b))


def step(book: us_orders.UsBook, price_a: int, price_b: int) -> None:
    """One cancel-replace cycle for one game: real inventory -> fresh quotes.
    The order layer snaps prices to the venue grid and posts post-only."""
    fv = fair_value(price_a, price_b)
    was = book.net_a
    book.poll_fills()
    if book.net_a != was:
        _log(f"FILL {book.slug}: net_a {was:+.1f} -> {book.net_a:+.1f} contracts")
    (pa, sa), (pb, sb) = quotes(fv, book.inventory_dollars(fv))
    book.cancel_all()                     # cancel-replace every tick
    posted = 0
    if sa > 0 and book.post("A", pa, sa):        # buy A YES @ pa
        posted += 1
    if sb > 0 and book.post("B", 1.0 - pb, sb):  # buy B YES @ (1 - pb)
        posted += 1
    # the venue drops would-cross post-only orders silently (pilot session
    # 1) — orders.list is the only authority on what actually rests
    resting = book.open_count()
    if resting != posted:
        _log(f"VERIFY {book.slug}: {resting}/{posted} posted orders "
             "resting (silent post-only rejection or instant fill)")


def resolve_book(game: Game) -> us_orders.UsBook | None:
    """Match a Pinnacle game to its US moneyline market. None when no open
    US market covers it (or the long side can't be determined)."""
    market = us_market.find_market(game)
    if market is None:
        return None
    long = us_market.long_side(market, game.home)
    slug = market.get("slug")
    if long is None or not slug:
        return None
    return us_orders.UsBook(slug=slug, long_side=long, log=_log)


def book_mid(slug: str) -> float | None:
    """Venue mid in the LONG side's terms (raw instrument book); compare
    against fv via Quoter.fv_long_frame. None when the book is one-sided."""
    b = us_market.market_book(slug)
    if b["bids"] and b["asks"]:
        return (b["bids"][0][0] + b["asks"][0][0]) / 2.0
    return None


def _log(msg: str) -> None:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def allowlist() -> set[str]:
    """MACH_FIVE_SLUGS=slug1,slug2 restricts quoting to those markets.
    The pilot quotes ONE hand-picked game — without this the loop quotes
    every in-horizon game on the slate."""
    raw = os.environ.get("MACH_FIVE_SLUGS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def shutdown(quoters: dict[str, "Quoter"], log=_log) -> None:
    """Best-effort cancel of every tracked market on the way out. An exit
    that skips this leaves resting bids unmanaged — no skew, no guard —
    until the venue's auto-cancel at game start, which can be hours away."""
    for slug, q in quoters.items():
        try:
            q.book.cancel_all()
            log(f"shutdown: canceled {slug} (net_a={q.book.net_a:+.1f})")
        except Exception as e:
            log(f"shutdown: CANCEL FAILED {slug}: {e} — check the venue UI")


class Quoter:
    """One game under management: book + guard + tape stats + quote state."""

    def __init__(self, game: Game, book: us_orders.UsBook):
        self.game = game
        self.book = book
        self.guard = GameGuard(commence_ts=game.commence_time.timestamp())
        self.tape = TapeStats(f"{book.slug}#A")
        self.quoting = False
        self.last_lineup_poll = 0.0
        self.last_reason = ""
        self.last_fv = fair_value(game.price_home, game.price_away)

    def fv_long_frame(self, fv_a: float) -> float:
        """fv is P(home)=A; the venue book is in LONG terms."""
        return fv_a if self.book.long_side == "A" else 1.0 - fv_a


def run() -> None:
    if os.environ.get("MACH_FIVE_LIVE") != "1":
        raise SystemExit(
            "mach_five.run() places REAL orders. Set MACH_FIVE_LIVE=1 only "
            "after the NEXT_STEPS gate is cleared, at pilot size.")

    lock = threading.Lock()
    quoters: dict[str, Quoter] = {}      # slug -> Quoter
    keys: dict[str, str] = {}            # market_id|commence -> slug
    skipped: set[str] = set()
    allow = allowlist()
    _log(f"allowlist: {', '.join(sorted(allow))}" if allow else
         "allowlist EMPTY (MACH_FIVE_SLUGS unset): quoting every "
         "in-horizon game")
    # SIGTERM must run the finally below, same as Ctrl-C's KeyboardInterrupt
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    def on_trade(trade: dict) -> None:
        """WS thread: fast pull on tape bursts — seconds, not next tick."""
        with lock:
            q = quoters.get(str(trade.get("marketSlug")))
        if q is None:
            return
        line = us_market.trade_line(trade, q.book.long_side,
                                    f"{q.book.slug}#A", f"{q.book.slug}#B")
        if line is None:
            return
        reason = q.tape.on_trade(line)
        if reason and q.quoting:
            q.guard.mark_toxic(time.time(), reason)
            try:
                q.book.cancel_all()
                q.quoting = False
                _log(f"PULLED {q.book.slug}: {reason}")
            except Exception as e:
                _log(f"pull failed {q.book.slug}: {e}")

    def hold(q: Quoter, reason: str) -> None:
        """Pull resting quotes (if any) and log why we're standing down."""
        if q.quoting:
            try:
                q.book.cancel_all()
            except Exception as e:
                _log(f"cancel failed {q.book.slug}: {e}")
            q.quoting = False
        _log(f"holding {q.book.slug}: {reason}")

    key_id, secret = us_market.creds()
    feed = us_market.TradeFeed(key_id, secret, on_trade, log=_log)
    feed.start()

    try:
        _run_loop(quoters, keys, skipped, allow, lock, feed, hold)
    finally:
        with lock:
            shutdown(quoters)   # Ctrl-C / SIGTERM / crash: leave no orphans


def _run_loop(quoters: dict[str, Quoter], keys: dict[str, str],
              skipped: set[str], allow: set[str], lock: threading.Lock,
              feed: us_market.TradeFeed, hold) -> None:
    """run()'s forever-loop, split out so its exit (whatever the cause)
    always flows through run()'s finally-shutdown."""
    while True:
        now = time.time()
        try:
            games = pinnacle_moneylines()
        except Exception as e:
            _log(f"odds poll failed: {e}")
            games = {}

        seen: set[str] = set()
        for market_id, game in games.items():
            if game.commence_time is None:
                continue
            to_pitch = game.commence_time.timestamp() - now
            key = f"{market_id}|{game.commence_time:%Y%m%d%H%M}"
            if not (0 < to_pitch < HORIZON_H * 3600) or key in skipped:
                continue
            slug = keys.get(key)
            if slug is None:
                try:
                    book = resolve_book(game)
                except Exception as e:
                    _log(f"resolve failed {market_id}: {e}")
                    continue
                if book is None:
                    skipped.add(key)
                    continue
                slug = book.slug
                if allow and slug not in allow:
                    _log(f"skipping {slug}: not in allowlist")
                    skipped.add(key)
                    continue
                keys[key] = slug
                with lock:
                    quoters[slug] = Quoter(game, book)
                _log(f"tracking {market_id} -> {slug}")
            q = quoters[slug]
            seen.add(slug)

            # lineup gate + game-status inputs (keyless MLB Stats API)
            if now - q.last_lineup_poll >= LINEUP_POLL_SEC:
                q.last_lineup_poll = now
                try:
                    day = lineups.mlb_day(game.commence_time)
                    sched = lineups.schedule(day)
                    pk = lineups.find_game_pk(game, sched)
                    state = next((str(g.get("status", {}).get("detailedState", ""))
                                  for g in sched if g.get("gamePk") == pk), "")
                    was = q.guard.delayed
                    q.guard.delayed = any(w in state for w in
                                          ("Delayed", "Postponed", "Suspended"))
                    if q.guard.delayed and not was:
                        _log(f"game state '{state}' for {slug}")
                    if not q.guard.lineups_done and pk is not None:
                        home, away = lineups.lineups_posted(pk)
                        if home and away:
                            q.guard.on_lineups_confirmed()
                            _log(f"lineups confirmed for {slug}")
                except Exception as e:
                    _log(f"lineup poll failed {slug}: {e}")
            fv = fair_value(game.price_home, game.price_away)
            q.last_fv = fv
            q.guard.on_fv(fv, now)

            # guard decision + act
            try:
                mid = book_mid(slug)
            except Exception:
                mid = None                # book read hiccup: not a signal
            ok, reason = q.guard.decision(now, q.fv_long_frame(fv), mid,
                                          feed.age(now))
            if ok:
                try:
                    step(q.book, game.price_home, game.price_away)
                    if not q.quoting:
                        _log(f"QUOTING {slug}")
                    q.quoting = True
                except Exception as e:
                    _log(f"step failed {slug}: {e}")
            elif q.quoting or reason != q.last_reason:
                hold(q, reason)
            q.last_reason = reason

        # games that vanished from the odds response never reach the loop
        # body above — without this pass their guard would never run again
        # and resting quotes would sit against a delisted anchor (Pinnacle
        # pulled Dodgers-Cubs ~70min pre-pitch on 2026-08-03). The stale-
        # anchor check inside decision() does the actual pulling.
        with lock:
            missing = [q for s, q in quoters.items() if s not in seen]
        for q in missing:
            ok, reason = q.guard.decision(now, q.fv_long_frame(q.last_fv),
                                          None, feed.age(now))
            if not ok and (q.quoting or reason != q.last_reason):
                hold(q, reason)
            q.last_reason = reason

        # drop games past their pitch; belt-and-braces cancel (the venue
        # auto-cancels at start anyway). Leftover inventory is logged, not
        # auto-flattened (see module docstring)
        with lock:
            done = [s for s, q in quoters.items()
                    if now >= q.guard.commence_ts]
            for slug in done:
                q = quoters.pop(slug)
                try:
                    q.book.cancel_all()
                except Exception:
                    pass
                _log(f"done {slug}; net_a={q.book.net_a:+.1f} contracts")
            keys = {k: s for k, s in keys.items() if s in quoters}
            feed.watch(set(quoters))
        time.sleep(LOOP_SEC)
