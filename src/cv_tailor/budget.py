"""Monthly request counters for the metered job sources.

`MonthlyBudget` is the mechanism; `SerpBudget` and `JSearchBudget` are the two
quotas, each with its OWN counter file so neither can spend the other's
allowance. Everything below about flock, atomic writes and the month-rollover
auto-reset belongs to the shared mechanism and applies to both.

JSearch (OpenWebNinja) runs on six keys at 200 requests/month each = 1,200/mo
total. Nothing enforced that before: the sibling htgaj pipeline burned four of
its six keys in a single run and then mislabelled them as permanently
exhausted. JSearchBudget caps a runaway at 600/mo -- half the pool -- which is
still ~3x what sources.yaml actually spends (6 UK queries/day = ~186/mo), so
the cap bounds accidents without rationing normal use.

The SerpAPI free plan is 250 searches/month TOTAL, and it is SHARED with the
norina-jobs project (a separate daily scan run for Teodor's girlfriend).
norina-jobs self-caps at roughly 120/mo, so cv-tailor takes a 90/mo budget --
that leaves headroom for both projects plus manual testing, well under the
combined 250 cap.

SerpBudget persists a running count in a small JSON file, keyed by calendar
month: `{"month": "2026-07", "used": n}`. take() must be called once per
SerpAPI query BEFORE issuing it; a False return means this month's cap is
already spent and the caller must skip the query instead of firing it.

take()'s whole read-compare-increment-write is held under an exclusive
flock on a sibling `<path>.lock` file (fcntl.flock; POSIX-only), the same
pattern scout_queue.update_entry uses for jobs.json -- this makes it
race-safe: two concurrent processes (cv-tailor and norina-jobs share this
budget) can never both read the same `used` count and both pass the cap
check, so takes can neither overshoot the cap nor silently lose one
writer's increment. The actual file write still goes through a unique-tmp-
name + os.replace atomic write (the same pattern used by scout_queue.py's
_write_atomic), so a crash mid-write can never corrupt the counter either.
Together these give take() BOTH guarantees: crash-safe (atomic write) AND
race-safe under concurrent writers (the flock) -- not just the crash-safety
a plain atomic write alone would provide.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


class MonthlyBudget:
    """Flock-guarded, atomically-written, month-keyed request counter.

    Subclasses supply the counter file name and the cap; the mechanism (and
    every guarantee documented in the module docstring) is shared."""

    DEFAULT_FILENAME = "budget.json"
    DEFAULT_CAP = 0
    LABEL = "budget"

    def __init__(self, path: Path | None = None, monthly_cap: int | None = None):
        self.path = (Path(path) if path is not None
                     else ROOT / "data" / self.DEFAULT_FILENAME)
        self.monthly_cap = self.DEFAULT_CAP if monthly_cap is None else monthly_cap

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
        can skip the query and log what got dropped.

        Race-safe: the read-compare-increment-write below runs under an
        exclusive flock on a sibling `<path>.lock` file, held from before
        the read to after the atomic write, so two concurrent processes can
        never both read the same `used` count and both proceed past the cap
        check (see the module docstring)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                data = self._read()
                if data["used"] >= self.monthly_cap:
                    return False
                data["used"] += 1
                self._write_atomic(data)
                return True
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


class SerpBudget(MonthlyBudget):
    """SerpAPI's 90/mo slice of a 250/mo plan shared with norina-jobs."""

    DEFAULT_FILENAME = "serpapi_budget.json"
    DEFAULT_CAP = 90
    LABEL = "serpapi"


class JSearchBudget(MonthlyBudget):
    """JSearch's own counter: 600/mo of a 1,200/mo six-key pool."""

    DEFAULT_FILENAME = "jsearch_budget.json"
    DEFAULT_CAP = 600
    LABEL = "jsearch"


class ApifyResultBudget(MonthlyBudget):
    """Metered by RESULT, not by request.

    SerpBudget and JSearchBudget count requests because those APIs price per
    request. cheap_scraper/linkedin-job-scraper prices per ROW, so a single run
    returning 200 rows costs 200 units while a request counter would report 1.
    On a FREE tier with $5/month that difference is the whole ballgame.

    Its own counter file, for the reason the module docstring gives for
    splitting serpapi from jsearch: a shared file lets one source quietly spend
    another's allowance.

    Adds a DAILY leg on top of the monthly one. Without it the month's whole
    allowance goes in the first week and the remaining three weeks find
    nothing -- the daily cap is what spreads coverage across the month.
    """

    DEFAULT_FILENAME = "apify_result_budget.json"
    DEFAULT_CAP = int(os.environ.get("APIFY_RESULT_CAP", "600"))
    LABEL = "apify"
    DEFAULT_DAILY_CAP = int(os.environ.get("APIFY_DAILY_RESULT_CAP", "25"))

    def __init__(self, path: Path | None = None, monthly_cap: int | None = None,
                 daily_cap: int | None = None):
        super().__init__(path=path, monthly_cap=monthly_cap)
        self.daily_cap = self.DEFAULT_DAILY_CAP if daily_cap is None else daily_cap

    def _read(self) -> dict:
        """The base class's month-keyed state plus a day-keyed counter that
        resets on an ISO-date change -- the same stale-key auto-reset, one
        level finer. A corrupt or missing file reads back as a fresh day."""
        data = super()._read()
        today = date.today().isoformat()
        stored: dict = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    stored = loaded
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                stored = {}
        same_day = (stored.get("day") == today
                    and stored.get("month") == data["month"])
        data["day"] = today
        data["day_used"] = int(stored.get("day_used", 0)) if same_day else 0
        return data

    def _headroom(self, data: dict) -> int:
        return max(0, min(self.monthly_cap - data["used"],
                          self.daily_cap - data["day_used"]))

    def _with_lock(self, fn):
        """Same exclusive flock discipline as take(): held from before the
        read to after the atomic write, so two processes can never both read
        the same counts and both proceed past the cap."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def reserve(self, n: int) -> int:
        """Grant up to `n` results, charging the WHOLE grant immediately.

        Fails CLOSED on purpose: the grant is held for the duration of the run,
        so a crash, a client timeout, or a killed process between the POST and
        settle() leaves the worst case charged. Charging after the rows come
        back would under-count in exactly the situation where the run was most
        expensive. Returns 0 when there is no headroom, and the caller must
        then make NO network call at all."""
        want = max(0, int(n))
        if want == 0:
            return 0

        def _do() -> int:
            data = self._read()
            granted = min(want, self._headroom(data))
            if granted <= 0:
                return 0
            data["used"] += granted
            data["day_used"] += granted
            self._write_atomic(data)
            return granted

        return self._with_lock(_do)

    def settle(self, granted: int, actual: int) -> None:
        """Refund the part of a grant the run did not use.

        Never refunds more than was granted, so a run that somehow returns more
        rows than reserved cannot hand back allowance that was never taken."""
        refund = max(0, int(granted) - max(0, int(actual)))
        if refund == 0:
            return

        def _do() -> None:
            data = self._read()
            data["used"] = max(0, data["used"] - refund)
            data["day_used"] = max(0, data["day_used"] - refund)
            self._write_atomic(data)

        self._with_lock(_do)
