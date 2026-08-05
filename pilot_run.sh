#!/bin/bash
# One-shot pilot launcher — 2026-08-04, White Sox @ Red Sox.
#   launch 19:40 UTC (3:40pm ET), first pitch 23:11 UTC (7:10pm ET),
#   SIGTERM 23:26 UTC -> mach_five's finally-shutdown cancels everything.
# Runs detached (nohup); all output in pilot.log next to this script.
cd /Users/isaiahreed/Projects/mach-five || exit 1
PY=/Users/isaiahreed/.pyenv/versions/3.12.3/bin/python3
LAUNCH=1785872400   # 2026-08-04 19:40:00 UTC
STOP=1785885960     # 2026-08-04 23:26:00 UTC

log() { echo "[launcher $(date -u '+%H:%M:%S')] $*" >> pilot.log; }

log "armed: launch at 19:40Z, stop at 23:26Z, slug aec-mlb-cws-bos-2026-08-04"
while [ "$(date +%s)" -lt "$LAUNCH" ]; do sleep 20; done

log "starting mach_five.run()"
MACH_FIVE_LIVE=1 MACH_FIVE_SLUGS=aec-mlb-cws-bos-2026-08-04 \
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
