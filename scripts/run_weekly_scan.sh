#!/usr/bin/env bash
# Wrapper for launchd: loads .env, activates venv, runs the weekly scanner,
# logs to a dated file, and shows a macOS notification when done.
set -euo pipefail

REPO="$HOME/repos/cv-tailor"
cd "$REPO"

LOG_DIR="$REPO/scans/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%F).log"

{
  echo "=== $(date -u +%FT%TZ) run_weekly_scan.sh start ==="

  # Load environment (.env has Azure OpenAI + Sheets creds)
  set -a
  source "$REPO/.env"
  set +a

  # Activate venv and run
  source "$REPO/.venv/bin/activate"
  python "$REPO/scripts/weekly_scan.py" --min-score 7 --max-results 10
  RC=$?

  echo "=== exit code: $RC ==="

  if [[ $RC -eq 0 ]]; then
    DIGEST="$REPO/scans/$(date +%F).md"
    if [[ -f "$DIGEST" ]]; then
      COUNT=$(grep -cE "^## [0-9]+" "$DIGEST" || echo 0)
      osascript -e "display notification \"$COUNT new candidates. Open scans/$(date +%F).md\" with title \"cv-tailor weekly scan\""
    fi
  else
    osascript -e "display notification \"Scan failed (exit $RC). See log.\" with title \"cv-tailor weekly scan\""
  fi
} >> "$LOG" 2>&1
