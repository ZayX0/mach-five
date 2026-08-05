# Next steps — after adequate data is gathered

Gate: do none of this until the recording campaign (a week-plus of slates) and
`replay.py`'s markout tables give a positive verdict on when/where quoting is
profitable. Going live before the timing answer exists just donates spread to
informed flow.

## Which Polymarket? (read this first)

Polymarket runs TWO separate exchanges with SEPARATE order books:

- **International** (polymarket.com): self-custody wallets, USDC on Polygon,
  EIP-712 signing via py-clob-client. This is the venue this repo's READ path
  (clob.polymarket.com / Gamma) and the recorder capture. US persons cannot
  trade it.
- **Polymarket US** (polymarket.us): CFTC-regulated, KYC via the iOS app,
  CUSTODIAL — there is no wallet, no signer key, and nothing to export. Its
  API uses Ed25519-signed requests with portal-issued credentials. This is
  the venue we can actually trade.

Consequence: recordings from 2026-08-01 onward capture the US venue
(`us_market.py`); the day-one international recordings are archived in
`recordings/intl/` — Pinnacle-fv timing conclusions from them transfer, but
don't mix the two venues in one replay run.

## Step-by-step: API access for market making (Polymarket US)

Source: https://docs.polymarket.us (api-reference/authentication, trader
guide; read 2026-07-31).

1. **Dedicate the bankroll.** Funding is in dollars through the app
   (custodial — no wallet). Every resting bid reserves its full cost, no
   leverage: 15 games x 2 sides x $100 = up to $3,000 reserved at peak.
2. **iOS app: account + identity verification.** The app is the only
   onboarding surface (KYC: SSN, photo ID, proof of address). Already done
   for the existing account.
3. **Create API credentials in the developer portal** (DONE 2026-07-31):
   polymarket.us/developer, sign in with the app credentials, create an API
   key -> **Key ID** + **Secret Key**. The secret is shown ONCE.
4. **Store secrets in the macOS Keychain, NOT `.env`** (DONE 2026-07-31):
   services `mach-five-key-id` and `mach-five-secret-key`. Code reads them at
   runtime via `us_market.creds()` (`security find-generic-password`) so they
   never touch disk in plaintext; `recorder._SECRETS` scrubs both values from
   every log line. `.env` holds only `ODDS_API_KEY`. If the key is ever
   rotated, update the two Keychain items — no code or file changes.
5. **Install the official SDK** (DONE 2026-08-01): `pip install
   polymarket_us`. Auth is Ed25519 request signing (headers `X-PM-Access-Key`
   / `X-PM-Timestamp` / `X-PM-Signature`); the SDK handles it, and
   `us_market.TradeFeed` already uses it for the trade WebSocket. No gas, no
   token approvals, no proxy wallets.
6. **Wire the order layer to the US SDK** (DONE 2026-08-02): `us_orders.py`
   — post-only GTC limits via `client.orders.create` (tick-snapped,
   integer contracts, A-frame -> intent mapping), `cancel_market` via
   `orders.cancel_all(slugs=[...])`, real `UsBook.poll_fills` from
   `portfolio.positions`. `mach_five.py` now discovers via
   `us_market.find_market` and refuses to run without `MACH_FIVE_LIVE=1`.
   Still missing before live: quote window (lineup-gated, from the replay
   verdict) and the toxic-flow guard — neither is in the loop yet.
7. **Prove it end-to-end with one $1 order** (DONE 2026-08-02, orders
   BMJPF7YWGBA3 / BMJP0H20YBAC on aec-mlb-bos-lad): placed far-from-touch
   post-only, confirmed resting via orders.list AND visible in the public
   book, modified, canceled, funds intact. KEY FINDING: the venue prices
   every order in the LONG side's terms — BUY_SHORT @ px rests as a long-
   frame SELL at px, so buying the short side at its own price p must
   submit (1 - p). The original assumption (short side priced in its own
   terms) was WRONG; `us_orders.place_bid` was fixed and re-proven live
   (intended Dodgers bid 0.44 -> landed exactly at long ask 0.56).
   Still unverified (needs a real fill): `portfolio.positions` keyed by
   slug — confirm during the first pilot session before trusting the skew.
   Note: the public book lags order changes by ~10-20s; use orders.list /
   orders.retrieve as the authority, not the book.
8a. **Pilot session 1 RUN 2026-08-04** (CWS@BOS, 40/100, allowlist, timed
   launcher): clean end to end. Gate opened at launch+3min, 3.5h of
   two-sided cancel-replace, zero fills (pessimistic queue, one game —
   normal), net_a +0.0, tape guard's first live firing (an $8,007 print,
   pulled in seconds via the WS callback), clean SIGTERM shutdown, $0 P&L,
   funds intact. FINDING -> next code change: the venue rejects
   would-cross post-only orders SILENTLY (no exception) — while Pinnacle
   led the venue by more than the half-spread (~1h mid-session) the A-side
   bid never rested and nothing logged. ~~`place_bid` must inspect the
   create response and surface non-resting orders~~ DONE 2026-08-04:
   two-layer fix — `create_rejected` screens every create response
   (best-effort; rejection shape not fully mapped) and `step()` now
   verifies via `open_orders`/`open_count` (orders.list, the authority)
   after posting, logging `VERIFY x/y resting` on any mismatch.
   Positions-keyed-by-slug still unverified (needs a fill) — carries to
   session 2.
8b. **Pilot session 2 armed 2026-08-04** for the 08-05 slate: the goal is
   the FIRST REAL FILL (session 1's zero fills were game selection — the
   08-04 paper replay at pilot sizing shows 15-16 pessimistic-bound fills
   on the slate's volume leaders vs ~1 on CWS-BOS). New `pilot_pick.py`
   ranks the slate by `stats.notionalTraded` at decision time and
   `pilot2_run.sh` (detached, same pattern as session 1) consumes it:
   decide 18:00Z (2pm ET) -> launch at leader's first pitch - 3.5h ->
   SIGTERM at pitch + 15min. Launch with
   `nohup ./pilot2_run.sh > /dev/null 2>&1 &`. On the first fill, run the
   step-8 verification checklist (positions keyed by slug, poll_fills
   latency, skew direction).

8. **First live session at pilot size**: DONE 2026-08-04 in code —
   `BASE_SIZE = 40` and `MAX_INVENTORY = 100` are set in `mach_five.py`
   (2:5 ratio kept; worst case $180 committed against $250 funding;
   `replay.py` pins research sizing 2000/5000 so paper tables keep their
   scale). ONE game via
   `MACH_FIVE_SLUGS=<slug>` (the allowlist — without it the loop quotes
   every in-horizon game), quote-on window from the
   replay verdict, stop at first pitch (Polymarket auto-cancels all resting
   sports orders at official scheduled start — but pull quotes on any delay:
   MLB `detailedState` "Delayed"/"Postponed" via `lineups.py`'s API).
   Exit (Ctrl-C/SIGTERM/crash) cancels all tracked markets via the
   finally-shutdown; if the log shows "CANCEL FAILED", check the venue UI.
   In-pilot verification checklist (the reason the pilot exists): on the
   first real fill, confirm `portfolio.positions` is keyed by slug
   (`net_a_shares`'s unverified assumption), that `poll_fills` sees it
   within a tick, and that both quotes then skew the right direction.

(The old EIP-712 / py-clob-client / allowances path applies only to the
international venue and was removed; see git/file history if ever needed.)

## Linux server migration (prepared 2026-08-05)

Repo-side work is DONE: git repo (initial commit cd84e65, `.gitignore`
excludes `.env`/`recordings/`/logs), `us_market.creds()` auto-selects the
systemd-credentials backend when `$CREDENTIALS_DIRECTORY` is set (Keychain
otherwise), `replay.py` reads `.jsonl.gz`, `archive_recordings.py` +
`deploy/` units handle the local-disk -> NFS nightly archive (recorder
writes local so an NFS stall can never cost tape; 8GB internal is plenty
at ~150MB/day live). Server-side checklist, in order:

1. Push to a PRIVATE remote (GitHub private or a bare repo on the server
   over SSH); clone on the server.
2. `python3 -m pip install requests python-dotenv polymarket_us websockets`
   (match the laptop's versions); copy `.env` (ODDS_API_KEY only) by hand.
3. Venue creds: `systemd-creds encrypt` the two values into
   `/etc/mach-five/*.cred` (or root-owned 0600 plain files with
   `LoadCredential=`) — names `mach-five-key-id` / `mach-five-secret-key`,
   matching what `creds()` reads. NEVER `.env`.
4. Mount the NFS at `/mnt/nfs/mach-five` (fstab, hard mount) and rsync the
   laptop's `recordings/` there once (keep `intl/` with it).
5. Edit paths/user in `deploy/*.service` to the server's layout, install
   all three units, `systemctl enable --now` the recorder service and the
   archive timer. `for f in tests/check_*.py; do python3 $f; done` on the
   server first — all offline, no key needed.
6. Fix the BSD-ism before any server pilot: `date -u -r "$EPOCH"` in
   pilot2_run.sh is macOS-only (Linux: `date -u -d "@$EPOCH"`). Deferred
   on 2026-08-05 because the script was mid-execution (editing a running
   bash script corrupts it). Better: fold the launcher into a small
   Python script — cross-platform, and the pick/launch/watchdog logic
   gets a check script.
7. CUTOVER IN ONE MOTION: `launchctl bootout gui/$(id -u)/com.mach-five.recorder`
   on the laptop, then start the server unit — two two-speed pollers
   would blow the 20k/month Odds API quota. Verify with
   `tail -f recorder.log` on the server and one `pinnacle_moneylines()`
   smoke call. Retire the laptop launchd plist after a clean server day.
8. Live sessions move last: run one paper day on the server (recorder +
   replay), then the next pilot launches from the server with the same
   MACH_FIVE_LIVE/MACH_FIVE_SLUGS gates.

## Also pending (from earlier findings)

- ~~Record Polymarket US market data~~ DONE 2026-08-01: `recorder.py` now
  records the US venue via `us_market.py` (keyless gateway books + authed
  trade WebSocket; Keychain creds). International recordings archived under
  `recordings/intl/`. Calibration DONE 2026-08-02: trade `size` = CONTRACTS
  (tape sums match sharesTraded deltas within ~1% on pre-game markets), so
  price*size = $ and no code change was needed; `stats.notionalTraded` is
  in CENTS. In-play tape undercounts ~2x — pre-game analysis unaffected.
  Maker and taker fees measured at 0 bps (proof-order commission fields).
- ~~Odds API quota fix~~ DONE 2026-08-02: two-speed odds polling
  (`recorder.odds_interval` — 60s from 1.5h before a first pitch to 0.5h
  after, 600s otherwise; ~14–18k calls/month).
- Back up `recordings/` off-machine; gzip closed recordings.
- ~~News/toxic-flow guard for the live loop~~ DONE 2026-08-02: `guard.py`
  (lineup gate + fv settle, tape-burst fast pull, fv-vs-mid divergence,
  dead-man on feed silence, game-status pull, cooldowns), wired into
  `mach_five.run()`. ~~Re-base `TAPE_ONESIDED_MAX`/`PRINT_MAX`~~ DONE
  2026-08-04 via `sweep.py` (offline threshold calibration over 41 games):
  raised to 120s x $6000 / $5000 — the old values fired ~2x/game on noise
  and caught none of the 13 real gated-window repricings. The same run
  validated `DIVERGENCE_MAX=0.02` (p99 calm divergence 1.39c),
  `HALF_SPREAD=0.006` (1 tick gets picked off: 1800s markout -$1,652),
  the settle grid (flat — current 3 x 0.002 fine), and kept
  `STOP_BEFORE_PITCH_MIN=2` (late-fill "toxicity" at the 1800s horizon is
  in-game fv contamination; clean +0.5..0.7c/share at 300s). Still before
  pilot: decide the end-of-window inventory policy (close_position vs
  hold; currently logged only) — inventory held through pitch is exposed
  to exactly the in-game variance the contaminated table was measuring.
