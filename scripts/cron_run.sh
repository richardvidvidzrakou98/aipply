#!/usr/bin/env bash
# Aipply scheduled run — generates application materials (NO auto-submit).
# You review materials on the dashboard (http://localhost:8092) and click
# "🚀 Apply" per job. To disable all runs:  touch .stop   (resume: rm .stop)
set -u
cd "$(dirname "$0")/.."

# Only one run at a time
exec 9>/tmp/aipply.lock
flock -n 9 || { echo "[cron] another run in progress — skipping"; exit 0; }

# Display for the browser (cron has no session environment)
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"

# Emergency stop check
if [ -f .stop ]; then
  echo "[cron] .stop present — run skipped"
  exit 0
fi

# Rotate keyword each run so every 2h cycle covers a different role
KEYWORDS=("devops engineer" "cloud engineer" "backend developer" "site reliability engineer")
HOUR=$(date +%s)
KW="${KEYWORDS[$(( (HOUR / 7200) % ${#KEYWORDS[@]} ))]}"
LIMIT="${AIPPLY_LIMIT:-3}"

echo "[cron] $(date '+%F %T') scanning: $KW (Remote), limit $LIMIT"
.venv/bin/python scripts/quick_run.py "$KW" "Remote" --limit "$LIMIT" --dry-run
echo "[cron] $(date '+%F %T') done"
