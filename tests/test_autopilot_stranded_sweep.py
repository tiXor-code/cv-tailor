"""Autopilot startup sweep for entries stranded mid-flight.

scripts/apply_approved.py walks approved -> assembling -> sending -> sent.
If the orchestrator is killed between those writes the entry keeps its
mid-flight status forever: the daily pass skips it (it is not `pending`),
the expiry sweep skips it (assembling/sending are not EXPIRABLE_STATUSES),
and only a human digging through jobs.json ever finds it. Shipped debt from
the 2026-07-23 autopilot ship.

The sweep is deliberately asymmetric, because the two stranded statuses
carry very different risk:

  assembling  Nothing has left the machine -- assembly only renders a CV and
              a cover letter. Safe to put back to `approved` and retry.

  sending     A send may ALREADY have reached a real employer, and the crash
              could have landed between the send and the ledger write. There
              is no safe automatic retry here. Never re-queue: reconcile
              against the applications ledger when it proves the send landed,
              otherwise park at needs_human for a human to check. Applying to
              the same employer twice is worse than applying zero times.

Fresh mid-flight entries are left alone so the sweep can never steal a job
from an orchestrator that is still legitimately working on it.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cv_tailor.autopilot import STRANDED_AFTER, run_autopilot

NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
TODAY = "2026-07-28"


def _write_day(root: Path, day: str, entries: list[dict]) -> None:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps(entries))


def _stale_ts() -> str:
    return (NOW - STRANDED_AFTER - timedelta(minutes=5)).isoformat()


def _fresh_ts() -> str:
    return (NOW - timedelta(minutes=1)).isoformat()


def _entry(i, status, changed_at, **over):
    base = {"id": f"job-{i}", "title": f"Role {i}", "company": f"Co{i}",
            "score": 9, "status": status, "decided_at": None,
            "status_changed_at": changed_at,
            "apply_method": "email", "apply_target": "hr@example.invalid",
            "url": f"https://example.invalid/{i}"}
    base.update(over)
    return base


def _read(root, day):
    return {e["id"]: e for e in json.loads((root / day / "jobs.json").read_text())}


def _noop_runner(day, job_id):
    return 0


def _run(root, *, ledger=(), runner=_noop_runner):
    """Run a pass with an injected ledger membership check."""
    return run_autopilot(NOW, queue_dir=root, runner=runner,
                         ledger_has=lambda job_id: job_id in set(ledger))


# --- assembling: safe to retry ---------------------------------------------

def test_stale_assembling_is_requeued_for_retry(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, "assembling", _stale_ts())])
    report = _run(tmp_path)
    e = _read(tmp_path, TODAY)["job-1"]
    assert e["status"] == "approved"
    assert e["error"] == "requeued-after-strand"
    assert [x["id"] for _, x in report.stranded] == ["job-1"]


def test_fresh_assembling_is_left_alone(tmp_path):
    """An orchestrator still working must never have its job stolen."""
    _write_day(tmp_path, TODAY, [_entry(1, "assembling", _fresh_ts())])
    report = _run(tmp_path)
    assert _read(tmp_path, TODAY)["job-1"]["status"] == "assembling"
    assert report.stranded == []


# --- sending: never auto-retry ---------------------------------------------

def test_stale_sending_with_ledger_row_is_reconciled_to_sent(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, "sending", _stale_ts())])
    _run(tmp_path, ledger=["job-1"])
    e = _read(tmp_path, TODAY)["job-1"]
    assert e["status"] == "sent"
    assert e["error"] == "reconciled-after-strand"


def test_stale_sending_without_ledger_row_parks_for_a_human(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, "sending", _stale_ts())])
    _run(tmp_path, ledger=[])
    e = _read(tmp_path, TODAY)["job-1"]
    assert e["status"] == "needs_human"
    assert e["error"] == "stranded-sending-unverified"


def test_stranded_sending_is_never_requeued(tmp_path):
    """The double-apply guard. A stranded send must not become approved
    again under ANY ledger state, because the send may already have landed."""
    for ledger in ([], ["job-1"]):
        root = tmp_path / f"case{len(ledger)}"
        _write_day(root, TODAY, [_entry(1, "sending", _stale_ts())])
        _run(root, ledger=ledger)
        assert _read(root, TODAY)["job-1"]["status"] != "approved"


def test_fresh_sending_is_left_alone(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, "sending", _fresh_ts())])
    _run(tmp_path)
    assert _read(tmp_path, TODAY)["job-1"]["status"] == "sending"


# --- scope + idempotence ----------------------------------------------------

def test_terminal_and_pending_statuses_are_untouched(tmp_path):
    _write_day(tmp_path, TODAY, [
        _entry(1, "sent", _stale_ts()),
        _entry(2, "failed", _stale_ts()),
        _entry(3, "rejected", _stale_ts()),
        _entry(4, "needs_human", _stale_ts()),
        _entry(5, "preview_sent", _stale_ts()),
    ])
    report = _run(tmp_path)
    q = _read(tmp_path, TODAY)
    assert [q[f"job-{i}"]["status"] for i in range(1, 6)] == [
        "sent", "failed", "rejected", "needs_human", "preview_sent"]
    assert report.stranded == []


def test_sweep_is_idempotent(tmp_path):
    _write_day(tmp_path, TODAY, [
        _entry(1, "assembling", _stale_ts()),
        _entry(2, "sending", _stale_ts()),
    ])
    _run(tmp_path)
    first = _read(tmp_path, TODAY)
    second_report = _run(tmp_path)
    second = _read(tmp_path, TODAY)
    assert {k: v["status"] for k, v in first.items()} == \
           {k: v["status"] for k, v in second.items()}
    # job-1 went back to `approved`, so the *pass* may act on it again, but
    # the sweep itself must find nothing still stranded.
    assert second_report.stranded == []


def test_sweep_runs_before_new_candidates_are_approved(tmp_path):
    """A stranded job must be resolved before the pass starts new work, so a
    crash can never silently accumulate mid-flight entries across days."""
    order = []

    def runner(day, job_id):
        order.append(job_id)
        return 0

    _write_day(tmp_path, TODAY, [
        _entry(1, "assembling", _stale_ts()),
        _entry(2, "pending", _stale_ts()),
    ])
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner,
                           ledger_has=lambda job_id: False)
    assert [x["id"] for _, x in report.stranded] == ["job-1"]
    # job-1 was requeued to `approved` by the sweep; the pass only auto-approves
    # `pending`, so the freshly requeued job is not double-run in the same pass.
    assert "job-2" in order
