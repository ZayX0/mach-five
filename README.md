# Mach Five

An automated market maker for MLB moneylines on **Polymarket US** (the
CFTC-regulated exchange), plus the research pipeline used to decide *when*
it should quote.

## How it works

Every MLB game is one binary market: side A (home team wins) and side B
(away), with `price(A) + price(B) = 1`. The strategy:

1. Take Pinnacle's moneyline (the sharpest sportsbook price), remove its
   vig, and treat the result as fair value (`P(home wins)`).
2. Rest a post-only buy order on **each** side, priced a half-spread away
   from fair value, so the pair costs less than $1. When both sides fill,
   that's a complete set redeemable for $1 — the difference is captured
   spread, regardless of who wins the game.
3. Skew: when one side fills first, both quotes shift and resize to make
   the market more likely to fill the other side, driving inventory back
   to flat.
4. Guard: quotes only rest when it's safe. They turn on after both
   starting lineups are confirmed *and* Pinnacle has digested them, and
   they are pulled within seconds on one-sided trade bursts, fair-value
   vs. market divergence, data-feed silence, game delays, or the approach
   of first pitch. (Recorded data shows quoting *into* pending lineups
   loses ~1.6¢/share to informed flow; quoting after them earns the
   spread.)

Sports markets on this venue trade pre-game for our purposes: the exchange
auto-cancels all resting orders at scheduled start, so the whole cycle
lives in the final hours before first pitch.

## The three programs

| Program | What it does |
|---|---|
| `recorder.py` | 24/7 daemon: records Pinnacle fair value, Polymarket US order books, the live trade tape, and lineup-posting events into `recordings/MM-DD-YYYY/*.jsonl` (one file per game, one folder per slate day). |
| `replay.py` | Offline analysis of those recordings: market quality by time-to-pitch, a paper-trading sweep over quote-start strategies (including the lineup-gated one) at both optimistic and pessimistic queue-position bounds, and fill markout tables — the adverse-selection detector. |
| `mach_five.py` | The live quoting loop (quotes + skew + guard + order layer). **Gated:** refuses to run without `MACH_FIVE_LIVE=1`, because it places real orders with real dollars. |

Supporting modules, one concern each: `odds_feed` (Pinnacle via The Odds
API), `fair_value` (devig), `us_market` (US venue discovery, books, trade
WebSocket), `us_orders` (order placement/cancel/positions via the official
SDK), `lineups` (confirmed-lineup detection, keyless MLB Stats API),
`guard` (quote/pull decisions), `paper_book` (simulated maker fills).
`clob.py`/`game_match.py` are the *international* Polymarket venue's read
path, kept for reference only.

## Running it locally

### Prerequisites

- Python 3.12+
- `pip install requests python-dotenv polymarket_us`
- **The Odds API key** (theoddsapi.com) in a `.env` file at the repo root:
  `ODDS_API_KEY=...`  — budget note: one Pinnacle poll costs 1 credit and
  the recorder polls two-speed (60s near first pitches, 600s otherwise),
  ~14–18k calls/month against a 20k/month plan.
- **Polymarket US API credentials** (polymarket.us/developer, requires an
  account KYC'd through the iOS app) stored in the **macOS Keychain** —
  never in `.env`:

  ```sh
  security add-generic-password -a "$USER" -s mach-five-key-id -w '<KEY_ID>'
  security add-generic-password -a "$USER" -s mach-five-secret-key -w '<SECRET>'
  ```

  Book snapshots are keyless; the credentials are needed for the trade
  WebSocket and (live only) order placement. Values are scrubbed from all
  log output.

### Tests

No framework — each module has a matching offline check script (canned
payloads, injected transports, no network, no real orders):

```sh
for f in tests/check_*.py; do python3 $f; done
```

### Record market data

Foreground / one-off:

```sh
python3 recorder.py run
```

For 24/7 operation install it as a launchd agent (survives crashes and
restarts at login; wraps the process in `caffeinate -i` so the machine
doesn't idle-sleep — note a closed laptop lid still sleeps):

```sh
cp com.mach-five.recorder.plist ~/Library/LaunchAgents/  # edit paths first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mach-five.recorder.plist
# restart after a code change:
launchctl kickstart -k gui/$(id -u)/com.mach-five.recorder
# stop for real (a plain `kill` triggers a supervised restart):
launchctl bootout gui/$(id -u)/com.mach-five.recorder
```

Logs go to `recorder.log` (UTC, time-only). Each game's JSONL carries
`meta`, `pinnacle` (fair value), `book` (both sides, A=home frame),
`trade` (deduped tape, exchange timestamps), and `lineup` lines; files
resume cleanly if the recorder restarts. Trade `size` is in contracts, so
`price × size` is dollars.

### Analyze

```sh
python3 replay.py recordings          # everything (skips recordings/intl)
python3 replay.py recordings/08-02-2026   # one slate day
```

Read the sweep tables as a bracket: the OPTIMISTIC table assumes front-of-
queue fills (upper bound), the PESSIMISTIC table joins behind all displayed
size and nobody ever cancels (lower bound). Judge strategies by **markout**
(fair-value drift after each fill), not raw P&L — negative markout means
the fills were informed flow picking the quotes off. The `lu-gated` row is
the lineup-gated strategy the live loop implements.

### Go live (don't, yet)

`mach_five.run()` is intentionally hard to start:

```sh
MACH_FIVE_LIVE=1 MACH_FIVE_SLUGS=aec-mlb-xxx-yyy-2026-08-05 \
  python3 -c "import mach_five; mach_five.run()"
```

`MACH_FIVE_SLUGS` (comma-separated) restricts quoting to those markets;
leaving it unset quotes every game inside the 8-hour horizon — never do
that on a first session. Exiting (Ctrl-C) cancels all resting orders on
the way out; if the log prints "CANCEL FAILED", verify in the venue UI.

Before ever setting that flag, read `NEXT_STEPS.md` — the go-live gate and
pilot runbook live there (the live constants are pilot-sized at
`BASE_SIZE=40` / `MAX_INVENTORY=100`; `replay.py` pins research sizing so
the paper tables keep their scale), along with what the $1 proof order
already verified about the venue's order semantics. Orders are always post-only limit
orders, so the loop can never accidentally cross the spread — but it can
absolutely lose money if run outside the gate's conditions.

## Repo conventions

- One responsibility per file; cross-module reuse via plain imports.
- Offline checks live in `tests/check_<module>.py`, never inline in
  modules.
- Deliberate shortcuts are marked with `ponytail:` comments naming the
  ceiling and the upgrade path — grep for them.
- API keys never appear in logs (`odds_feed._redact_key`,
  `recorder._SECRETS`); US credentials live only in the Keychain.

See `CLAUDE.md` for the deeper architectural notes and `NEXT_STEPS.md` for
the current state of the go-live sequence.
