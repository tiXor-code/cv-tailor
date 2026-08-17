#!/usr/bin/env bash
# Wrapper for launchd (com.teodorlutoiu.cvtailor.daily): loads .env, activates
# the venv, runs the v2 funnel scanner daily, logs to a dated file, and shows a
# macOS notification when done. Replaces run_weekly_scan.sh.
set -uo pipefail

REPO="$HOME/repos/cv-tailor"
cd "$REPO" || exit 1

LOG_DIR="$REPO/scans/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%F).log"

{
  echo "=== $(date -u +%FT%TZ) run_scan.sh start ==="

  # Load environment (.env has Azure OpenAI + Sheets + Telegram creds).
  set -a
  source "$REPO/.env"
  set +a

  source "$REPO/.venv/bin/activate"
  # --min-score is the queue-entry floor (what gets written for review), NOT the
  # apply floor -- autopilot below applies only at/above SCOUT_AUTO_APPROVE_MIN.
  # This value must track scan.py's argparse default; the explicit flag wins, so
  # editing only the default there changes nothing about this 09:00 run.
  python "$REPO/scripts/scan.py" --min-score 6 --max-results 10
  RC=$?
  echo "=== exit code: $RC ==="

  # Scout autopilot (spec 2026-07-23): the ONLY thing that approves and applies.
  # Auto-approves at/above SCOUT_AUTO_APPROVE_MIN (floor 6), sweeps stranded and
  # stale entries, sends the digest. The scan above only writes the queue.
  # Gated INSIDE the script by SCOUT_AUTOPILOT in .env; safe to call always.
  python "$REPO/scripts/autopilot.py"
  echo "=== autopilot exit code: $? ==="

  DIGEST="$REPO/scans/$(date +%F).md"
  if [[ $RC -eq 0 && -f "$DIGEST" ]]; then
    COUNT=$(grep -cE "^## [0-9]+" "$DIGEST" || echo 0)
    osascript -e "display notification \"$COUNT new candidates. scans/$(date +%F).md\" with title \"cv-tailor daily scan\"" 2>/dev/null || true
  fi
} >> "$LOG" 2>&1
