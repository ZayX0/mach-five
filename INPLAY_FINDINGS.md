Both measurements are done — 79 unique games across 08-01 → 08-06, 239 timed scoring plays. Script is at `scratchpad/inplay_probe.py`.

Three bugs had to be fixed before the numbers meant anything: `playEvents[0].startTime` carries a pre-game timestamp (3–4h early) so first pitch comes from `plays[0].about.startTime`; 18 of 97 recordings are duplicates of the same `gamePk`; and venue notional is `instrument_px × size`, not the A-frame price.

## M1 — there is no spread to make

| min from first pitch | games | snaps | median spread | p90 | % at 1 tick | % ≥2c |
|---|---|---|---|---|---|---|
| −360..−120 | 76 | 64,995 | 0.50c | 0.50c | 99.9% | 0.0% |
| −60..−30 | 76 | 8,163 | 0.50c | 0.50c | 99.8% | 0.0% |
| −10..0 | 77 | 2,675 | 0.50c | 0.50c | 100.0% | 0.0% |
| 0..30 | 78 | 8,055 | 0.50c | 0.50c | 94.9% | 2.0% |
| 60..90 | 76 | 7,913 | 0.50c | 0.50c | 93.2% | 2.5% |
| 120..150 | 69 | 6,003 | 0.50c | 0.50c | 91.9% | 2.6% |

`TICK` is 0.005, so **0.50c is a locked one-tick market** — the tightest state that can exist. It holds 100% of the time pre-game and ~93% in-play. Going in-play buys you a spread that is wider than one tick only ~2–3% of the time, and never systematically. Splitting by how lopsided the game is changes nothing (94.0% / 92.3% / 93.4% one-tick).

That kills the in-play thesis on its own: a fv model accurate to ~1c has no room in a 0.5c market. But the same table says something sharper about what's already running — `HALF_SPREAD = 0.006` posts a two-sided quote **1.2c wide into a 0.5c market**. Our bid is ~0.35c below the best bid, which snaps to at least one tick behind the touch. Arithmetically, mach-five can never join the touch, let alone lead it, in either regime.

There's a second finding I'd treat as suggestive rather than settled. When the best bid falls, I compared the displayed size at that level against the size that actually printed there: median **111 contracts printed vs ~146,562 displayed**. Levels are pulled, not traded through. Two caveats — book `qty` units aren't calibrated against trade `size` (my calibration attempt was too noisy at 15s book cadence to pin the scale), and conditioning on "the bid fell" selects for swept levels and ignores persistent ones. But the unit-free half stands: ~111 contracts is all that changes hands at a dying level, so fills anywhere in that queue are rare events. That is a plausible mechanism for session 3's zero fills, and it is a pre-game problem, not an in-play one.

## M2 — you lose the race by roughly 10 seconds

Timed from **pitch release** — the first instant the outcome is knowable to anyone. Median move on a scoring play is 10.0c (p90 22.0c), and 97% go the direction the run implies, so the events are real.

```
time from pitch release to 50% of the move   (n=237)
  p10 +2.5s   p25 +3.9s   med +7.2s   p75 +12.8s   p90 +20.5s
    moved within  5s of release:  33%
    moved within 10s of release:  66%
    moved within 15s of release:  81%
```

Median release → play end (runs actually in) is **17.9s**. The market is half-repriced at +7.2s. So the venue completes half its move *before the play is even over*, and:

- **79% of scoring plays are already past the 50% mark by the time StatsAPI logs the play** (median −9.5s relative to `endTime`).
- 85% are past the 25% mark.

`endTime` is the earliest instant a poller could react — MLB's publish lag and your own poll interval come **on top of that**. So a 1-second GUMBO poller would arrive, at the median, after most of the move. The fastest repricings started 1.4–2.0s after release, on moves of 10–32c.

Two biases, both pointing the same way: the WS tape captures only ~71% of venue notional (p25 0.59, p75 0.94), so the true first print at a new level is often one we never recorded — meaning the real latency is *worse* than measured. And I anchored on release rather than contact, which is generous by another second or two.

## Verdict

Both measurements fail, independently and decisively. M1 says there's no spread to capture; M2 says you'd be picked off in the 7 seconds before you could even see the event. Inning-break-only quoting survives M2 by construction, but M1 kills it anyway — a locked one-tick book pays nothing for sitting through a break.

I'd stop the in-play line here rather than build `win_prob.py`. What I would chase instead, in order:

1. **The queue question, properly.** It's the live constraint on the strategy you already have, and M1 raises it without answering it. Calibrating book `qty` against trade `size` needs book snapshots faster than 15s at least for a sample of games — a cheap recorder change, keyless, no API spend.
2. **`HALF_SPREAD` vs. a one-tick market.** 0.6c can't reach the touch. Whether any value can, and at what adverse-selection cost, is the question the pilot was implicitly asking.
3. **The duplicate recordings.** 18 of 97 files over six days are the same `gamePk` twice. Any `replay.py` table over 08-01…08-06 double-weights those games; dedupe by `gamePk` keeping the longer file.

I haven't touched anything in the repo — the probe is scratchpad-only. Say the word if you want it promoted alongside `replay.py`/`sweep.py` with a `tests/check_` script, or the dedupe fix applied.
