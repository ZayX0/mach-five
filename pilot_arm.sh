#!/bin/bash
# Arm a pilot session at $100 funding: 10/25 sizing (2:5 ratio, worst
# case 2 x $45 = $90 committed), top-2 volume picks in one process.
# Launch via the NEXT_STEPS step-8 systemd-run pattern (credentials via
# LoadCredentialEncrypted; KillMode=mixed so a `systemctl stop` TERMs
# the launcher only and the pilot's finally-shutdown gets to cancel):
#
#   sudo systemd-run --unit=mach-five-pilot --collect --uid=<user> \
#     -p WorkingDirectory=<repo> \
#     -p KillMode=mixed -p TimeoutStopSec=45 \
#     -p LoadCredentialEncrypted=mach-five-key-id:/etc/mach-five/key-id.cred \
#     -p LoadCredentialEncrypted=mach-five-secret-key:/etc/mach-five/secret-key.cred \
#     <repo>/pilot_arm.sh
#
# Launcher arc + pilot output land in pilot.log (repo root).
cd "$(dirname "$0")" || exit 1
export MACH_FIVE_BASE=10 MACH_FIVE_MAX=25
exec .venv/bin/python3 pilot_launch.py --games 2
