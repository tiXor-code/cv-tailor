"""Monthly counter for SerpAPI queries.

The SerpAPI free plan is 250 searches/month TOTAL, and it is SHARED with the
norina-jobs project (a separate daily scan run for Teodor's girlfriend).
norina-jobs self-caps at roughly 120/mo, so cv-tailor takes a 90/mo budget --
that leaves headroom for both projects plus manual testing, well under the
combined 250 cap.

SerpBudget persists a running count in a small JSON file, keyed by calendar
month: `{"month": "2026-07", "used": n}`. take() must be called once per
SerpAPI query BEFORE issuing it; a False return means this month's cap is
already spent and the caller must skip the query instead of firing it.
Writes are atomic (unique tmp name + os.replace) so a crash mid-write can
never corrupt the counter or race a concurrent reader/writer -- the same
pattern used by scout_queue.py's _write_atomic.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


class SerpBudget:
    def __init__(self, path: Path | None = None, monthly_cap: int = 90):
        self.path = Path(path) if path is not None else ROOT / "data" / "serpapi_budget.json"
        self.monthly_cap = monthly_cap

    def _read(self) -> dict:
        """Current month's state. A missing file, corrupt file, or a stale
        month (the file was written in a previous calendar month) all read
        back as a fresh 0-used month -- that IS the auto-reset."""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if data.get("month") == _current_month():
                    return {"month": data["month"], "used": int(data.get("used", 0))}
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        return {"month": _current_month(), "used": 0}

    def _write_atomic(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
        tmp_path.write_text(json.dumps(data))
        os.replace(tmp_path, self.path)

    def used(self) -> int:
        return self._read()["used"]

    def take(self) -> bool:
        """Consume one query from this month's budget. Returns False (and
        leaves the stored count untouched) once the cap is spent, so callers
        can skip the query and log what got dropped."""
        data = self._read()
        if data["used"] >= self.monthly_cap:
            return False
        data["used"] += 1
        self._write_atomic(data)
        return True
