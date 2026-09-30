#!/bin/bash
# tools/run_poll.sh -- cron wrapper for `python -m sender poll`.
#
# Writes every run as a clearly-delimited section into logs/YYYY-MM-DD.log
# (server-local time) so runs can be tracked individually within a day.
# Daily files older than 30 days are pruned. Delimiters are ASCII only.
#
# Cron usage (the interpreter MUST be the 3.12 one; the server's default
# python3 is 3.6.8 and must not be used):
#
#   */5 * * * * PYTHON=/opt/hc_python/bin/python3.12 \
#     /home/aizewlkt/aizenpaper.com/invoice-automated-reporting/tools/run_poll.sh \
#     --once --timeout 240
#
# Manual rehearsal (offline, cannot send):
#
#   PYTHON=.venv/bin/python tools/run_poll.sh --once --stub --dry-run
#
# All arguments are forwarded verbatim to `python -m sender poll`.
#
# Timestamps are server-local. To name files in UTC instead, prefix the cron
# entry with TZ=UTC.

set -u

# Deterministic PATH for cron's minimal environment.
export PATH=/usr/bin:/bin

# Resolve the project root from this script's own location (tools/ -> root)
# so the script works from any checkout without hardcoded paths.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR" || exit 1

PYTHON="${PYTHON:-python3}"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

# Retention: drop daily logs older than 30 days. Only touches logs/*.log.
find "$LOG_DIR" -maxdepth 1 -name '*.log' -type f -mtime +30 -delete 2>/dev/null

DAY="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/$DAY.log"

{
    echo "================================================================================"
    echo "RUN START  $(date '+%Y-%m-%d %H:%M:%S %Z (%z)')"
    echo "COMMAND    $PYTHON -m sender poll $*"
    echo "--------------------------------------------------------------------------------"
} >> "$LOG_FILE"

START_EPOCH="$(date +%s)"
"$PYTHON" -m sender poll "$@" >> "$LOG_FILE" 2>&1
RC=$?
END_EPOCH="$(date +%s)"
ELAPSED=$((END_EPOCH - START_EPOCH))

{
    echo "--------------------------------------------------------------------------------"
    echo "RUN END    $(date '+%Y-%m-%d %H:%M:%S %Z (%z)')  exit=$RC  elapsed=${ELAPSED}s"
    echo "================================================================================"
} >> "$LOG_FILE"

exit "$RC"