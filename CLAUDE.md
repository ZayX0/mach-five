# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Polymarket US market-maker for MLB moneylines. It quotes both sides of
a game's binary market using a sharp (Pinnacle) price as fair value, captures
the spread, and skews on inventory. Fair-value, read, and order paths are all
real; going live is GATED (see "Real vs. gated" below).

Provenance: the odds/devig/CLOB/game-matching code is ported from the sibling
repo `../mlb-prop-finder` (`src/propfinder/market/*` and
`scripts/probe_exchanges.py`). When touching devig or the game matcher, that
repo is the reference implementation.

## Commands

No build system — plain Python scripts, one concern per module.

- **Run one module's checks:** `python3 tests/check_<module>.py` — every
  module has a matching check script in `tests/` with `assert`s. This is the
  test suite; there is no pytest. Checks run offline (canned payloads /
  injected transport / pre-seeded caches), so they need no network or API
  key. Test code never lives in the live modules.
- **Run all checks:** `for f in tests/check_*.py; do python3 $f; done`
- **Live feed test (needs key):** requires `ODDS_API_KEY` in `.env` (loaded via
  python-dotenv). E.g. `python3 -c "from odds_feed import pinnacle_moneylines; print(len(pinnacle_moneylines()))"`.
- **Record market data:** runs as the launchd agent `com.mach-five.recorder`
  (`~/Library/LaunchAgents/com.mach-five.recorder.plist`: KeepAlive restart
  on crash, starts at login, wraps the recorder in `caffeinate -i`; logs to
  recorder.log, timestamps UTC time-only). After a code change:
  `launchctl kickstart -k gui/$(id -u)/com.mach-five.recorder`. Stop for
  real: `launchctl bootout gui/$(id -u)/com.mach-five.recorder` (a plain
  `kill` just triggers a supervised restart). Analyze with
  `python3 replay.py recordings` (expands day folders — `.jsonl` and
  archived `.jsonl.gz` alike — skips `intl/`).
- **Archive closed days:** `python3 archive_recordings.py recordings <dest>`
  gzip-moves day folders older than today UTC (and quiet 6h+) to bulk
  storage; on the Linux server this runs nightly via `deploy/` systemd
  units (see NEXT_STEPS "Linux server migration").
- Dependencies: `requests`, `python-dotenv` (installed in the environment; no
  requirements file yet).

When adding non-trivial logic, extend the module's `tests/check_<module>.py`
with an offline assertion rather than adding a test framework.

## Data flow

`mach_five.run()` is the loop. Per tick, per game:

```
odds_feed.pinnacle_moneylines()   The Odds API /odds, bookmakers=pinnacle, markets=h2h
   -> Game(home, away, price_home, price_away, commence_time)
fair_value.fair_prob(a, b)        American -> implied prob -> devig -> P(home wins)
mach_five.quotes(fv, inventory)   fv +/- HALF_SPREAD, shifted by inventory skew
us_market.find_market/long_side   Pinnacle game -> US market slug + which side is long
us_orders.UsBook                  post-only bids / cancel / real positions (SDK)
```

## Core domain model

- A game is one **binary market** with complementary outcomes: `price(A) + price(B) = 1`.
- **Side A = home, side B = away** — this convention is fixed across every
  module (`Game.price_home` is A, `_extract`/`find_tokens` assign home→A).
- You only ever **buy YES**. "Selling A" == "buying B". Profit = assemble a
  complete set (1 A + 1 B) for under $1 and redeem for $1. `quotes()` posts a
  buy on each side priced so the pair costs < $1.
- **Inventory skew** (`quotes()` in `mach_five.py`) is the risk control: being
  long one side lowers both quotes and shrinks that side's order size to drive
  the position back to flat. This is the real logic — its `_check()` asserts the
  skew direction on both signs of inventory.
- **`BASE_SIZE` and `MAX_INVENTORY` are coupled by ratio** (2:5). Skew and
  size-shaping act on `inventory / MAX_INVENTORY`, so rescaling
  `BASE_SIZE` alone leaves the skew decorative — rescale
  `MAX_INVENTORY` proportionally. `HALF_SPREAD`/`SKEW_STRENGTH` are in price
  units and don't rescale. The LIVE constants in `mach_five.py` are
  PILOT-sized (40/100, for $250 funding); `replay.py` pins research sizing
  (2000/5000) at import so every paper table keeps its historical scale —
  a deliberate split, change both only deliberately.

## Real vs. gated — know the boundary

- **Real & live-verified:** the Pinnacle feed (`odds_feed`), devig
  (`fair_value`), US market discovery + books + trade WS (`us_market`),
  lineup detection (`lineups`).
- **Real & live-verified: the order layer** (`us_orders.py`, official
  `polymarket_us` SDK; $1 proof orders 2026-08-02). Post-only GTC limits
  (`participateDontInitiate`), bids tick-snapped down (TICK = 0.005),
  integer contracts, A-frame -> the single US instrument via intent
  (BUY_LONG when `side == long_side`, else BUY_SHORT). CRITICAL, proven
  live: the venue prices EVERY order in the LONG side's terms — buying the
  short side at its own price p submits (1 - p); `place_bid` handles the
  flip, never bypass it. `UsBook.poll_fills` reads real positions; the
  positions-keyed-by-slug assumption still needs a real fill to confirm.
  The public book lags order changes ~10-20s; `orders.list`/`retrieve` is
  the authority.
- **Gated:** `mach_five.run()` refuses to start without `MACH_FIVE_LIVE=1`
  in the env — it places real orders with real dollars. Do not set it until
  the NEXT_STEPS gate (campaign verdict) is cleared, and only at pilot size
  (`BASE_SIZE=40` + `MAX_INVENTORY=100`, already set) with `MACH_FIVE_SLUGS=<slug>`
  restricting quoting to hand-picked market(s) — unset means the whole
  slate. Any exit (Ctrl-C, SIGTERM, crash) runs a finally-shutdown that
  cancels every tracked market so no unmanaged order outlives the loop.
- **Quote window + toxic-flow guard live in `guard.py`** (pure logic, no
  I/O — `mach_five.run()` feeds it): quotes rest only after both lineups
  are confirmed AND fv settled (`LINEUP_SETTLE_*`, shared with replay's
  `lu-gated` row so backtest and live agree), and are pulled on tape
  bursts (via the trade-WS callback — seconds, not next tick), fv-vs-mid
  divergence, feed silence (dead man), a stale fv anchor (`FV_STALE_SEC` —
  Pinnacle delists games pre-pitch, seen live 2026-08-03; games that vanish
  from the odds response get their own decision pass in `run()` since they
  never reach the per-game loop body), game delay/postponement, or within
  2 min of scheduled start. Every toxic trigger starts a cooldown.
  Tape thresholds are in real dollars (units calibrated) and were re-based
  by `sweep.py` (2026-08-04, 41 games): print size is barely predictive
  inside gated windows, so they sit at tail-insurance levels — re-sweep as
  tape accumulates. `sweep.py` is the offline threshold-calibration tool
  (event studies + lu-gated paper re-runs per constant); the same run
  validated DIVERGENCE_MAX/HALF_SPREAD/settle params and showed the
  late-window "toxicity" at the 1800s horizon is in-game fv contamination
  (clean at 300s), so STOP_BEFORE_PITCH_MIN stays 2.
  Leftover inventory at the stop is logged, NOT auto-flattened.
- `clob.py`/`game_match.py` are the INTERNATIONAL venue's read path, kept
  for reference and for `clob._get` (the shared injectable transport);
  their order stubs are dead code for this repo's purposes.

## Paper trading (quote-timing research)

Three modules answer "when before first pitch should quoting turn on".

**Venue: everything live is Polymarket US** (`us_market.py` reads,
`us_orders.py` writes) — the CFTC-regulated exchange the user can trade.
`clob.py`/`game_match.py` remain the international venue's read path,
reference only (see NEXT_STEPS.md "Which Polymarket?"). US structure: ONE instrument per game (`marketSides`
long/short, `long_side` in meta says whether long = home); everything is
normalized back into the A=home frame so replay.py runs unchanged. Books are
keyless REST; trades come from the authed markets WebSocket (creds in the
macOS Keychain as `mach-five-key-id` / `mach-five-secret-key` — never .env,
scrubbed from logs). Units are calibrated (2026-08-02): trade `size` =
contracts, price*size = dollars, `stats.notionalTraded` is in CENTS; the
in-play tape undercounts ~2x (pre-game is complete) — see us_market.py's
docstring. Maker AND taker fees measured at 0 bps on this account.
Pre-switch international recordings live in `recordings/intl/`
(same format; don't mix venues in one replay run).

**API budget:** the account is capped at 20,000 Odds API calls/month; one
`pinnacle_moneylines()` poll = 1 call (the whole slate comes back in it).
The recorder polls two-speed (`odds_interval`): 60s while any known game is
inside its critical window (1.5h before first pitch to 0.5h after — where fv
resolution drives the markout tables), 600s otherwise, clamped so a sleep
never overshoots a known window entry. Typical slates land ~450–600
calls/day ≈ 14–18k/month. `mach_five.run()` still polls unconditionally —
fine for short live sessions, not for 24/7. Polymarket books and the trade
tape are keyless and unmetered.

- `recorder.py run` — daemon; per in-window game appends JSONL to
  `recordings/` (meta, Pinnacle fv, Polymarket book snapshots, deduped trade
  tape from `data-api.polymarket.com/trades?market=<conditionId>`). Resumes
  cleanly on restart. Trade lines carry the exchange timestamp, not poll time;
  the first tape poll backfills the market's last ~100 prints, so trades can
  predate the recording. Books/fv are observations and cannot be backfilled —
  a slate not recorded is gone. Also logs a `lineup` event per side on first
  observation of a confirmed lineup (via `lineups.py`, boxscore battingOrder,
  polled every 120s keyless); the ts is an upper bound on true posting time,
  and lineups already posted when recording starts are right-censored.
- `paper_book.py` — simulated maker fills: a resting bid fills only when a
  real print SELLs into it at/below our price after a latency delay. Fills at
  our price; conservative on when, optimistic on queue — paper P&L is an
  upper bound, judge by markout.
- `replay.py recordings/*.jsonl` — market-quality stats by
  minutes-to-first-pitch, plus a quote-start-time sweep driving
  `mach_five.quotes` through a `PaperBook`, scored by mark-to-fair P&L and
  fill markout (negative markout = picked off by informed flow). A final
  table re-buckets fill markout relative to lineup completion — if fv
  settling is "lineups posted" in disguise, quote timing should key on the
  lineup event, not the clock.

`game_match.find_market` returns the full Gamma market dict (the recorder
needs `conditionId`); `find_tokens` is now a thin wrapper over it.

## Game matching (the subtle part)

`game_match.find_tokens` maps a Pinnacle game to its Polymarket moneyline.
Doubleheaders are the trap: a makeup game's Polymarket event keeps the
**original date in its slug**, so the covered-game start parsed from a market's
resolution text ("scheduled for July 29 at 1:10PM ET") is the primary signal
and outranks the slug date. Falls back to slug-date, then nearest `startDate`,
only when no time is parseable. `pick_moneyline` avoids spreads/totals (which
reuse the same two-team outcomes) via `NON_MONEYLINE_MARKERS` and breaks ties by
volume. Team-name matching is exact-or-nickname (`_is_team`), never fuzzy.

## Conventions

- One responsibility per file; cross-module reuse is via plain imports
  (`game_match` and `mach_five` import helpers from `clob`).
- The `apiKey` query param is redacted from error text before it can reach logs
  (`odds_feed._redact_key`) — preserve this when touching HTTP error paths.
- Deliberate shortcuts are marked with `ponytail:` comments naming the ceiling
  and the upgrade path; grep for them to find what's intentionally deferred.
