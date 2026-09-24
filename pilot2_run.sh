#!/bin/bash
# One-shot pilot session 2 launcher — 2026-08-05, slug picked at decision
# time by pilot_pick.py (the slate's volume leader).
#   decide 18:00 UTC (2pm ET) -> launch at first pitch - 3.5h ->
#   SIGTERM at first pitch + 15min -> mach_five's finally-shutdown.
# Runs detached (nohup); all output in pilot.log next to this script.
cd "$(dirname "$0")" || exit 1
PY=${PY:-python3}
DECIDE=1785952800   # 2026-08-05 18:00:00 UTC

log() { echo "[launcher $(date -u '+%H:%M:%S')] $*" >> pilot.log; }

log "session 2 armed: picking the volume leader at 18:00Z on 2026-08-05"
while [ "$(date +%s)" -lt "$DECIDE" ]; do sleep 20; done

log "running pilot_pick.py"
PICK=$("$PY" pilot_pick.py 2>> pilot.log)
if [ -z "$PICK" ]; then
    log "ABORT: pilot_pick found no eligible game — no session today"
    exit 1
fi
SLUG=$(echo "$PICK" | awk '{print $1}')
PITCH=$(echo "$PICK" | awk '{print $2}')
LAUNCH=$((PITCH - 12600))   # first pitch - 3.5h
STOP=$((PITCH + 900))       # first pitch + 15min
log "picked $SLUG (pitch $(date -u -r "$PITCH" '+%H:%M:%S')Z);" \
    "launch $(date -u -r "$LAUNCH" '+%H:%M:%S')Z, stop $(date -u -r "$STOP" '+%H:%M:%S')Z"

while [ "$(date +%s)" -lt "$LAUNCH" ]; do sleep 20; done

log "starting mach_five.run()"
MACH_FIVE_LIVE=1 MACH_FIVE_SLUGS="$SLUG" \
  "$PY" -c "import mach_five; mach_five.run()" >> pilot.log 2>&1 &
PID=$!
log "pilot pid $PID"

while [ "$(date +%s)" -lt "$STOP" ] && kill -0 "$PID" 2>/dev/null; do sleep 20; done
if kill -0 "$PID" 2>/dev/null; then
    log "sending SIGTERM"
    kill -TERM "$PID"
    wait "$PID"
    log "pilot exited cleanly"
else
    wait "$PID" 2>/dev/null
    log "pilot exited on its own before the stop time — check pilot.log above"
fi
