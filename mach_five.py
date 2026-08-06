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
start unless MACH_FIVE_LIVE=1 is set. Sessions run at the coded pilot
sizing (or a MACH_FIVE_BASE/MACH_FIVE_MAX override — 2:5 ratio enforced;
rescale together or the skew is decorative), one or two games; keep
games x (2*BASE + MAX_INVENTORY) under the account funding. ponytail: inventory left at the stop is NOT
flattened automatically — close_position by hand or hold through
settlement; decide per NEXT_STEPS step 8.
"""
from __future__ import annotations
import json
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
# PILOT SIZING (2026-08-06, funding cut to $150): worst case = 2*BASE
# resting + MAX_INVENTORY held = $54/game — fits TWO games ($108, $42
# buffer) or one with room. BASE:MAX must stay 2:5 or the skew is
# decorative. MACH_FIVE_BASE/MACH_FIVE_MAX override per session (ratio
# enforced); research sizing (2000/5000) is pinned by replay.py so the
# paper tables keep their scale — rescale BOTH places deliberately.
BASE_SIZE = 12.0           # $ per side when flat
MAX_INVENTORY = 30.0       # $ of one-sided exposure before we stop adding
SKEW_STRENGTH = 0.004      # how hard inventory pushes quotes (in price units)
RESIZE_FRAC = 0.25         # requote when desired size drifts more than this
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


class Journal:
    """Per-session JSONL of per-tick quoting state — guard verdict, per-side
    keep/post decisions, resting prices — under journals/ next to the repo.
    PURE OBSERVABILITY: it changes no quoting behavior, and a write failure
    must never touch the loop, so every append is best-effort-silent.
    Analyzed offline by session_report.py."""

    def __init__(self, path: str | None = None):
        self.path = path            # resolved lazily on first write

    def write(self, obj: dict) -> None:
        try:
            if self.path is None:
                os.makedirs("journals", exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
                self.path = os.path.join("journals", f"session-{stamp}.jsonl")
            with open(self.path, "a") as f:
                f.write(json.dumps(obj) + "\n")
        except Exception:
            pass                    # observability never breaks quoting


def keep_quote(resting_price: float, resting_dollars: float, price: float,
               size: float, resize_frac: float | None = RESIZE_FRAC) -> bool:
    """Keep-if-unchanged requote policy, shared with replay's policy tables
    so backtest and live agree: hold a resting bid only when it already sits
    at the new snapped price and its size hasn't drifted more than
    `resize_frac` from desired (None = price-only, the research variant).
    Never hold a side the skew shut off (size <= 0). Replay verdict
    2026-08-06 (61 games, pessimistic queue): ~30x fewer posts, ~30% more
    fills, flat markout per filled $ vs cancel-replace-every-tick."""
    if size <= 0.0 or abs(resting_price - price) > us_orders.TICK / 2:
        return False
    return (resize_frac is None
            or abs(resting_dollars - size) <= resize_frac * size)


def step(book: us_orders.UsBook, price_a: int, price_b: int) -> dict:
    """One requote cycle for one game: real inventory -> fresh quotes,
    KEEP-IF-UNCHANGED per side (see keep_quote) — cancel-replacing every
    tick would surrender the queue spot, and queue position is the fill
    edge. Strays (resting orders we don't track) are swept every cycle;
    orders.list is the authority on what actually rests.

    Returns a per-tick state dict for the session journal (fv, net_a, and
    per side act/px/sz/since) — observability only, no caller keys
    behavior off it."""
    fv = fair_value(price_a, price_b)
    was = book.net_a
    book.poll_fills()
    if book.net_a != was:
        _log(f"FILL {book.slug}: net_a {was:+.1f} -> {book.net_a:+.1f} contracts")
    (pa, sa), (pb, sb) = quotes(fv, book.inventory_dollars(fv))
    want = {"A": (us_orders.snap_bid(pa), sa),          # buy A YES @ pa
            "B": (us_orders.snap_bid(1.0 - pb), sb)}    # buy B YES @ (1-pb)
    open_ids = book.open_ids()
    strays = book.cancel_strays(open_ids)
    if strays:
        _log(f"VERIFY {book.slug}: canceled {strays} untracked order(s)")
    kept = posted = 0
    sides: dict[str, dict] = {}
    for side in ("A", "B"):
        price, size = want[side]
        o = book.resting.get(side)
        if (o is not None and o["id"] in open_ids
                and keep_quote(o["price"], o["dollars"], price, size)):
            kept += 1                     # hold the queue spot
            sides[side] = {"act": "kept", "px": o["price"],
                           "sz": o["dollars"], "oid": o["id"],
                           "since": o.get("since")}
            continue
        book.cancel_side(side, open_ids)
        oid = book.post(side, price, size) if size > 0 else None
        if oid:
            posted += 1
        # act "none": degenerate size, or a create the venue rejected
        sides[side] = {"act": "posted" if oid else "none", "px": price,
                       "sz": size, "oid": oid,
                       "since": (book.resting.get(side) or {}).get("since")}
    out = {"fv": fv, "net_a": book.net_a, "kept": kept, "posted": posted,
           "strays": strays, "sides": sides}
    if posted:
        # the venue drops would-cross post-only orders silently (pilot
        # session 1) — orders.list is the only authority on what rests
        resting = book.open_count()
        out["open"] = resting
        if resting != kept + posted:
            _log(f"VERIFY {book.slug}: {resting}/{kept + posted} orders "
                 "resting (silent post-only rejection or instant fill)")
    return out


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


def size_override() -> tuple[float, float] | None:
    """Optional pilot sizing from the env: MACH_FIVE_BASE + MACH_FIVE_MAX
    (dollars). Both or neither, and the BASE:MAX ratio must match the
    coded constants (2:5) — skew and size-shaping act on
    inventory/MAX_INVENTORY, so a lone rescale leaves the skew
    decorative. Applied by run() only; replay's research pinning and the
    coded pilot defaults are untouched. Raises SystemExit on a bad
    override: refusing to start beats quoting at accidental size."""
    base, mx = os.environ.get("MACH_FIVE_BASE"), os.environ.get("MACH_FIVE_MAX")
    if base is None and mx is None:
        return None
    if base is None or mx is None:
        raise SystemExit("set BOTH MACH_FIVE_BASE and MACH_FIVE_MAX "
                         "(BASE:MAX must stay 2:5) or neither")
    try:
        b, m = float(base), float(mx)
    except ValueError:
        raise SystemExit(f"non-numeric MACH_FIVE_BASE/MAX: {base!r}/{mx!r}")
    if b <= 0 or m <= 0 or abs(b / m - BASE_SIZE / MAX_INVENTORY) > 1e-9:
        raise SystemExit(f"MACH_FIVE_BASE:MACH_FIVE_MAX must keep the "
                         f"{BASE_SIZE:g}:{MAX_INVENTORY:g} ratio "
                         f"(got {b:g}:{m:g})")
    return b, m


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
    global BASE_SIZE, MAX_INVENTORY
    if os.environ.get("MACH_FIVE_LIVE") != "1":
        raise SystemExit(
            "mach_five.run() places REAL orders. Set MACH_FIVE_LIVE=1 only "
            "after the NEXT_STEPS gate is cleared, at pilot size.")
    sized = size_override()
    if sized:
        BASE_SIZE, MAX_INVENTORY = sized
        _log(f"sizing override: BASE_SIZE={BASE_SIZE:g} "
             f"MAX_INVENTORY={MAX_INVENTORY:g} (worst case per game "
             f"~${2 * BASE_SIZE + MAX_INVENTORY:g})")

    lock = threading.Lock()
    quoters: dict[str, Quoter] = {}      # slug -> Quoter
    keys: dict[str, str] = {}            # market_id|commence -> slug
    skipped: set[str] = set()
    journal = Journal()
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
                journal.write({"type": "pull", "ts": time.time(),
                               "slug": q.book.slug, "reason": reason})
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
        _run_loop(quoters, keys, skipped, allow, lock, feed, hold, journal)
    finally:
        with lock:
            shutdown(quoters)   # Ctrl-C / SIGTERM / crash: leave no orphans


def _run_loop(quoters: dict[str, Quoter], keys: dict[str, str],
              skipped: set[str], allow: set[str], lock: threading.Lock,
              feed: us_market.TradeFeed, hold,
              journal: Journal | None = None) -> None:
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
                    res = step(q.book, game.price_home, game.price_away)
                    if not q.quoting:
                        _log(f"QUOTING {slug}")
                    q.quoting = True
                    if journal:
                        journal.write({"type": "tick", "ts": now,
                                       "slug": slug, "quoting": True, **res})
                except Exception as e:
                    _log(f"step failed {slug}: {e}")
            else:
                if q.quoting or reason != q.last_reason:
                    hold(q, reason)
                if journal:
                    journal.write({"type": "tick", "ts": now, "slug": slug,
                                   "quoting": False, "reason": reason,
                                   "fv": fv, "net_a": q.book.net_a})
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
            if journal and not ok:
                journal.write({"type": "tick", "ts": now, "slug": q.book.slug,
                               "quoting": False, "reason": reason,
                               "missing": True, "fv": q.last_fv,
                               "net_a": q.book.net_a})
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
