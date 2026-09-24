"""Autopilot policy: select at/above the floor, CAS approve, run orchestrator,
expire, digest.

The orchestrator is injected as `runner(scan_date, job_id) -> int` so no real
subprocess/browser/SMTP ever runs here; the fake runner mutates the queue the
way scripts/apply_approved.py would (status writes via update_entry).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cv_tailor.autopilot import (
    AUTO_APPROVE_MIN, EXPIRE_DAYS, AutopilotReport, auto_approve_min,
    build_digest, run_autopilot,
)
from cv_tailor.scout_queue import update_entry

NOW = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
TODAY = "2026-07-23"


@pytest.fixture(autouse=True)
def _floor_env_is_stated_not_inherited(monkeypatch):
    """Each test here declares the floor it is proving. A stray
    SCOUT_AUTO_APPROVE_MIN exported in the shell must never be what decides
    whether this suite passes."""
    monkeypatch.delenv("SCOUT_AUTO_APPROVE_MIN", raising=False)


@pytest.fixture(autouse=True)
def _fixed_fingerprint(monkeypatch):
    from cv_tailor import autopilot as ap
    monkeypatch.setattr(ap, "revive_fingerprint", lambda **kw: "fp-current")


def _write_day(root: Path, day: str, entries: list[dict]) -> None:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps(entries))


def _entry(i, score=8, status="pending", **over):
    base = {"id": f"job-{i}", "title": f"Role {i}", "company": f"Co{i}",
            "score": score, "status": status, "decided_at": None,
            "apply_method": "email", "apply_target": "hr@example.invalid",
            "url": f"https://example.invalid/{i}"}
    base.update(over)
    return base


def _read(root, day):
    return {e["id"]: e for e in json.loads((root / day / "jobs.json").read_text())}


def test_approves_only_at_or_above_the_floor_highest_first(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=5), _entry(2, score=6), _entry(3, score=9)])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert ran == ["job-3", "job-2"]  # score desc; the 5 untouched
    q = _read(tmp_path, TODAY)
    assert q["job-1"]["status"] == "pending"
    assert q["job-2"]["approved_by"] == "autopilot"
    assert q["job-2"]["decided_at"] is not None
    assert [e["id"] for _, e in report.applied] == ["job-3", "job-2"]
    assert [e["id"] for _, e in report.queued_new] == ["job-1"]


# --- the floor -----------------------------------------------------------

def test_floor_defaults_to_six():
    assert AUTO_APPROVE_MIN == 6
    assert auto_approve_min() == 6


def test_env_override_can_raise_the_floor_and_is_never_import_cached(tmp_path, monkeypatch):
    """SCOUT_AUTO_APPROVE_MIN is read fresh on every call, so a value set after
    this module was imported still governs the pass."""
    monkeypatch.setenv("SCOUT_AUTO_APPROVE_MIN", "9")
    assert auto_approve_min() == 9

    _write_day(tmp_path, TODAY, [_entry(1, score=8), _entry(2, score=9)])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert ran == ["job-2"]
    assert _read(tmp_path, TODAY)["job-1"]["status"] == "pending"


def test_nothing_below_six_is_ever_auto_approved(tmp_path, monkeypatch):
    """6 is a decision, not a setting: it is the lowest score Teodor is willing
    to have a real application sent for under his own name. The env var may
    raise that bar and never lower it, and no malformed value opens a hole --
    every one of these must clamp back to 6 with nothing below it approved or
    handed to the orchestrator."""
    _write_day(tmp_path, TODAY, [_entry(i, score=i) for i in range(6)])

    for value in (None, "5", "1", "0", "-1", "-999", "", " ", "5.9", "six",
                  "0x6", "1e9", "None", "6; rm -rf /"):
        if value is None:
            monkeypatch.delenv("SCOUT_AUTO_APPROVE_MIN", raising=False)
        else:
            monkeypatch.setenv("SCOUT_AUTO_APPROVE_MIN", value)
        assert auto_approve_min() >= 6, f"SCOUT_AUTO_APPROVE_MIN={value!r} lowered the floor"

        ran = []
        run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: ran.append(j) or 0)
        assert ran == [], f"SCOUT_AUTO_APPROVE_MIN={value!r} applied a sub-6 job: {ran}"
        statuses = {i: e["status"] for i, e in _read(tmp_path, TODAY).items()}
        assert set(statuses.values()) == {"pending"}, \
            f"SCOUT_AUTO_APPROVE_MIN={value!r} moved a sub-6 job: {statuses}"


def test_cas_conflict_skips_without_crash(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9)])

    calls = []

    def runner(day, job_id):  # pragma: no cover - must never run
        calls.append(job_id)
        return 0

    # Simulate Teodor's tap racing autopilot: entry already approved.
    update_entry(TODAY, "job-1", lambda e: e.update(status="approved"), queue_dir=tmp_path)
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert calls == []
    assert report.applied == [] and report.failed == []


def test_outcome_bucketing(tmp_path):
    _write_day(tmp_path, TODAY, [
        _entry(1, score=9), _entry(2, score=8), _entry(3, score=8), _entry(4, score=8)])
    outcome = {"job-1": "sent", "job-2": "needs_human", "job-3": "needs_review", "job-4": "failed"}

    def runner(day, job_id):
        update_entry(day, job_id, lambda e: e.update(status=outcome[job_id]), queue_dir=tmp_path)
        return 0 if outcome[job_id] != "failed" else 1

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert [e["id"] for _, e in report.applied] == ["job-1"]
    assert sorted(e["id"] for _, e in report.parked) == ["job-2", "job-3"]
    assert [e["id"] for _, e in report.failed] == ["job-4"]


def test_runner_exception_is_recorded_not_raised(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9)])

    def runner(day, job_id):
        raise RuntimeError("boom")

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert [e["id"] for _, e in report.failed] == ["job-1"]


def test_one_crashed_runner_does_not_stop_the_rest_of_the_pass(tmp_path):
    """Catching the exception is only half the property: the loop must go ON to
    the next candidate. With a single candidate (see the test above) a `break`,
    a re-raise or a `return` all still look like a pass, so the day's remaining
    approved jobs could be silently dropped by one crashed orchestrator. This
    is the two-candidate version, restoring what the deleted
    test_auto_apply_pending_continues_after_runner_failure proved on the old
    scan-side path."""
    _write_day(tmp_path, TODAY, [_entry(1, score=9), _entry(2, score=8)])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        if job_id == "job-1":
            raise RuntimeError("boom")
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert ran == ["job-1", "job-2"], "the crash ended the pass instead of skipping one job"
    assert [e["id"] for _, e in report.failed] == ["job-1"]
    assert [e["id"] for _, e in report.applied] == ["job-2"]
    # and the survivor was really applied, not just bucketed
    assert _read(tmp_path, TODAY)["job-2"]["status"] == "sent"


def test_expiry_sweep_boundary_and_statuses(tmp_path):
    old_day = (NOW - timedelta(days=8)).date().isoformat()
    edge_day = (NOW - timedelta(days=6)).date().isoformat()
    stale = NOW - timedelta(days=7, hours=1)
    fresh = NOW - timedelta(days=6)
    _write_day(tmp_path, old_day, [
        _entry(1, status="pending"),                                        # no stamp -> scan date -> expired
        _entry(2, status="needs_human", status_changed_at=stale.isoformat()),   # expired
        _entry(3, status="needs_review", status_changed_at=fresh.isoformat()),  # kept (recent change)
        _entry(4, status="sent"),                                           # terminal -> never expired
    ])
    # 6d old and below the floor -> kept by the sweep, not auto-approved
    _write_day(tmp_path, edge_day, [_entry(5, score=5, status="pending")])
    _write_day(tmp_path, TODAY, [])

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)
    assert sorted(e["id"] for _, e in report.expired) == ["job-1", "job-2"]
    q_old = _read(tmp_path, old_day)
    assert q_old["job-1"]["status"] == "rejected" and q_old["job-1"]["error"] == "auto_expired"
    assert q_old["job-3"]["status"] == "needs_review"
    assert q_old["job-4"]["status"] == "sent"
    assert _read(tmp_path, edge_day)["job-5"]["status"] == "pending"


def test_a_park_that_proves_no_submission_is_retried_once(tmp_path):
    """A park was NEVER retried, so every fix was retroactively useless.

    Measured 2026-09-16: Sardine (score 8), Checkly (7) and Flip (6) each
    parked `resume-upload-failed: no file input found` between 09-10 and
    09-16. The cause was that the Ashby form was not reachable -- fixed the
    same day at 13:50 (wait for render) and 14:53 (navigate to the application
    route) -- and a live probe afterwards found the resume input present on all
    three boards. Nothing ever re-attempted them. Same story for the arbeitnow
    jobs that parked `no-adapter` before greenhouse learned the EU board
    domain at 14:02.

    Safe BY CONSTRUCTION: only reasons that PROVE no submission are revived, so
    a retry can never duplicate a real application. Ambiguous parks, where the
    send may already have landed, must stay parked forever."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human",
               error="resume-upload-failed: no file input found"),
        _entry(2, score=8, status="needs_human",
               error="no-confirmation: submission may have succeeded, VERIFY on the portal"),
    ])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert ran == ["job-1"], "only a park proving no submission may be retried"
    q = _read(tmp_path, yesterday)
    assert q["job-1"]["status"] == "sent"
    assert q["job-1"].get("revived_at"), "the retry must be stamped, so it happens once"
    assert q["job-2"]["status"] == "needs_human", "ambiguous park must never be retried"


def test_a_needs_review_park_is_revived_now_that_the_gate_is_gone(tmp_path):
    """needs_review is set right after assembly, BEFORE any portal
    interaction, so it provably never submitted.

    It used to be excluded here on the grounds that a human gate Teodor had
    not ruled on must never be bypassed. He ruled on 2026-09-17 -- apply
    anyway, and Telegram him the letter -- but Cohere (score 8) and
    Mistral.ai (7) were already sitting in it, and nothing in autopilot
    advances that status: _sweep_expired can only reject it. A status nothing
    can advance is a dead end, not a review."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [_entry(1, score=8, status="needs_review")])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert ran == ["job-1"]
    assert _read(tmp_path, yesterday)["job-1"]["status"] == "sent"


def test_a_revived_park_is_not_revived_again(tmp_path):
    """One free retry per job, not a loop. Without the stamp a job that parks
    for an unfixed cause would revive, re-park and revive again every run,
    burning a browser launch each time, forever."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human", error="no-adapter",
               revived_at="2026-09-15T00:00:00+00:00", revived_for="no-adapter",
               revived_code="fp-current"),
    ])
    _write_day(tmp_path, TODAY, [])
    ran = []

    run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: ran.append(j) or 0)

    assert ran == []
    assert _read(tmp_path, yesterday)["job-1"]["status"] == "needs_human"


def test_a_stamp_from_before_reason_tracking_gets_one_grandfather_pass(tmp_path):
    """`revived_for` was added in the same session as the sweep, so entries
    revived just before it carry `revived_at` alone. Treating those as "already
    used your retry" would strand every one of them -- including jobs whose
    exact blocker was fixed minutes later, which is the failure this sweep
    exists to prevent.

    Bounded and self-correcting: the pass stamps revived_for, after which the
    same-wall rule applies normally and it cannot repeat."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human",
               error="unanswerable-required:Total years of experience",
               revived_at="2026-09-16T21:00:00+00:00"),
    ])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert ran == ["job-1"]
    assert _read(tmp_path, yesterday)["job-1"]["revived_for"] == (
        "unanswerable-required:Total years of experience")


def test_a_park_whose_reason_changed_earns_another_retry(tmp_path):
    """One retry per DISTINCT reason, not one ever.

    Measured 2026-09-16: the first revive moved Sardine from
    `resume-upload-failed` to `unanswerable-required:How did you hear about
    Sardine?` -- real progress, the widget bug gone and only a missing ANSWER
    left. The answer was written minutes later. Under a once-ever cap that fix
    could never reach the job it was written for, recreating exactly the
    "every fix is retroactively useless" problem this sweep exists to solve.

    A job re-parking at the SAME wall still gets nothing, so this cannot loop."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human",
               error="unanswerable-required:How did you hear about Sardine?",
               revived_at="2026-09-15T00:00:00+00:00",
               revived_for="resume-upload-failed: no file input found"),
        _entry(2, score=8, status="needs_human",
               error="no-adapter",
               revived_at="2026-09-15T00:00:00+00:00",
               revived_for="no-adapter",
               revived_code="fp-current"),
    ])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert ran == ["job-1"], "a changed reason means progress, so one more retry"
    q = _read(tmp_path, yesterday)
    assert q["job-1"]["revived_for"] == "unanswerable-required:How did you hear about Sardine?"
    assert q["job-2"]["status"] == "needs_human", "same wall twice: no further retry"


def test_same_wall_is_retried_once_the_code_or_answers_changed(tmp_path):
    """The same-wall rule assumed a wall that gave the same reason is unchanged
    -- but a FIX changes the wall. Live ElevenLabs (score 8): revived once for
    unwritable-required:Location, failed again on that same question (the bug
    was not fixed yet), and was then blocked for good -- minutes after the
    no-id combobox fix made its whole live form fill end to end.

    So the revive remembers a fingerprint of what decides outcomes (adapters,
    screening, apply_policy, answers.yaml) and retries when it changed."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human", error="unwritable-required:Location",
               revived_at="2026-09-20T00:00:00+00:00",
               revived_for="unwritable-required:Location", revived_code="fp-old"),
        _entry(2, score=8, status="needs_human", error="unwritable-required:Location",
               revived_at="2026-09-20T00:00:00+00:00",
               revived_for="unwritable-required:Location", revived_code="fp-current"),
    ])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert ran == ["job-1"], "fingerprint moved -> one more try; unchanged -> none"
    assert _read(tmp_path, yesterday)["job-1"]["revived_code"] == "fp-current"


def test_the_fingerprint_moves_with_answers_and_adapter_code(tmp_path, monkeypatch):
    """A new fact in answers.yaml is as much a changed wall as a code fix:
    Sardine was unblocked by adding in_person_interview, not by any code.

    Calls _compute_fingerprint directly: the module fixture stubs the public
    revive_fingerprint so every OTHER test is deterministic."""
    from cv_tailor import autopilot as ap
    src = tmp_path / "src" / "cv_tailor"; (src / "portal").mkdir(parents=True)
    (src / "screening.py").write_text("x = 1\n")
    (src / "apply_policy.py").write_text("y = 1\n")
    (src / "portal" / "ashby.py").write_text("z = 1\n")
    answers = tmp_path / "answers.yaml"; answers.write_text("a: 1\n")

    base = ap._compute_fingerprint(src_dir=src, answers_path=answers)
    assert base == ap._compute_fingerprint(src_dir=src, answers_path=answers), "stable"
    answers.write_text("a: 2\n")
    after_answers = ap._compute_fingerprint(src_dir=src, answers_path=answers)
    assert after_answers != base
    (src / "portal" / "ashby.py").write_text("z = 2\n")
    assert ap._compute_fingerprint(src_dir=src, answers_path=answers) != after_answers


@pytest.mark.parametrize("reason", [
    "submit-rejected: the portal explicitly refused the submission, nothing was sent",
    "captcha",
])
def test_a_portal_defence_is_never_retried_even_after_a_fix(tmp_path, reason):
    """proves_no_submission is the right test for ROLLBACK and the wrong one
    for RETRY. A spam flag or a captcha is the portal's own defence, not a bug a
    code change could fix -- and resubmitting to a portal that flagged the
    submission as spam is permanently out of scope.

    Live ElevenLabs (score 8), 2026-09-24: filled its whole form, clicked
    submit, and got "flagged as possible spam". Its reason had just CHANGED
    (Location -> submit-rejected), so without this the next 09:00 run would
    have resubmitted it automatically. Excluded even with a moved fingerprint."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human", error=reason),
        _entry(2, score=8, status="needs_human", error=reason,
               revived_at="2026-09-20T00:00:00+00:00",
               revived_for="unwritable-required:Location", revived_code="fp-old"),
    ])
    _write_day(tmp_path, TODAY, [])
    ran = []

    run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: ran.append(j) or 0)

    assert ran == []
    q = _read(tmp_path, yesterday)
    assert q["job-1"]["status"] == q["job-2"]["status"] == "needs_human"


def test_a_handoff_is_its_own_digest_section_not_a_failure(tmp_path):
    """A LinkedIn job handed to Teodor is neither applied nor failed. Without
    its own bucket, autopilot filed any unknown final status under Failed."""
    from cv_tailor.autopilot import build_digest
    _write_day(tmp_path, TODAY, [_entry(1, score=8), _entry(2, score=7)])

    def runner(day, job_id):
        status = "handed_off" if job_id == "job-1" else "pending"
        update_entry(day, job_id, lambda e: e.update(status=status), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)

    assert [e["id"] for _, e in report.handed_off] == ["job-1"]
    assert [e["id"] for _, e in report.deferred] == ["job-2"]
    assert report.failed == []
    text = build_digest(report)
    assert "New on your LinkedIn list (apply, then tick it) (1)" in text
    assert "Waiting for tomorrow's LinkedIn slots (1)" in text


def test_reviving_drops_the_phantom_ledger_row(tmp_path, monkeypatch):
    """Without this the feature is silently useless. Sardine's park left a row
    in the applications ledger (09:04 2026-09-16), and apply_approved refuses a
    job that already has one -- so a revived entry would be rejected as a
    duplicate and nothing would be gained. Dropping the row is safe for exactly
    the same reason the retry is: the reason PROVES nothing was sent."""
    from cv_tailor import autopilot as autopilot_mod

    forgotten = []
    monkeypatch.setattr(autopilot_mod, "_ledger_forget", forgotten.append)

    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [
        _entry(1, score=8, status="needs_human",
               error="unwritable-required:What would be your salary expectation for this role?"),
        _entry(2, score=8, status="needs_human", error="handoff-manual: no adapter"),
    ])
    _write_day(tmp_path, TODAY, [])

    run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)

    assert forgotten == ["job-1"], "only the proven-no-submission row is dropped"


def test_backlog_within_window_is_approved(tmp_path):
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [_entry(1, score=8)])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append((day, job_id))
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert ran == [(yesterday, "job-1")]
    assert [e["id"] for _, e in report.applied] == ["job-1"]


def test_queued_new_counts_only_todays_pendings(tmp_path):
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [_entry(1, score=5)])   # old sub-floor: not "new"
    _write_day(tmp_path, TODAY, [_entry(2, score=5)])
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)
    assert [e["id"] for _, e in report.queued_new] == ["job-2"]
    assert report.has_activity()  # a new sub-floor job queued for review IS activity


def test_no_activity_no_digest(tmp_path):
    _write_day(tmp_path, TODAY, [])
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)
    assert not report.has_activity()
    assert build_digest(report) is None


def test_digest_contents(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9), _entry(2, score=5)])

    def runner(day, job_id):
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    text = build_digest(report)
    assert "Co1" in text and "Role 1" in text
    assert "Co2" in text                       # queued, below the floor
    assert "admin.teodorlutoiu.com/scout" in text
    assert "—" not in text and "–" not in text  # no em/en dash


def test_notify_called_only_with_activity(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9)])
    sent = []

    def runner(day, job_id):
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner, notify=lambda t: sent.append(t) or True)
    assert len(sent) == 1

    sent2 = []
    _write_day(tmp_path, "2026-07-24", [])
    run_autopilot(NOW + timedelta(days=1), queue_dir=tmp_path, runner=lambda d, j: 0,
                  notify=lambda t: sent2.append(t) or True)
    assert sent2 == []


# --- expiry rolls back a ledger row that never meant anything -------------------
#
# The 2026-09-16 phantom rows: Flip GmbH, Checkly and Sardine each parked with
# resume-upload-failed -- an abort that happens before any submit click -- and
# each KEPT the ledger row apply_approved had pre-inserted. norm_key then
# blocked every same-company|role sibling as a duplicate, permanently, for
# applications that were never sent. Clearing them by hand needs a human; the
# sweep already visits exactly these entries, so it can clear them itself.

def _seed_ledger(db_path, job_id="job-1"):
    from cv_tailor import cache
    conn = cache.connect(db_path)
    cache.record_application(conn, job_id=job_id, company="Acme Inc.",
                             role="AI Engineer", url="https://example.test/1",
                             channel="portal")
    conn.close()


def _ledger_owns(db_path, job_id="job-1") -> bool:
    from cv_tailor import cache
    conn = cache.connect(db_path)
    try:
        return cache.own_application_recorded(conn, job_id)
    finally:
        conn.close()


def test_expiring_a_park_that_never_submitted_clears_its_ledger_row(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("SCOUT_DB_PATH", str(db))
    _seed_ledger(db)
    old_day = (NOW - timedelta(days=8)).date().isoformat()
    _write_day(tmp_path, old_day, [_entry(1, status="needs_human")])
    update_entry(old_day, "job-1",
                 lambda e: e.update(error="resume-upload-failed: no file input found"),
                 queue_dir=tmp_path)
    _write_day(tmp_path, TODAY, [])

    run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)

    assert _ledger_owns(db) is False, "a park that never submitted must not keep its row"


def test_expiring_an_ambiguous_park_keeps_its_ledger_row(tmp_path, monkeypatch):
    """no-confirmation means the submit may well have landed server-side.
    Deleting that row would let a genuine duplicate go out later."""
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("SCOUT_DB_PATH", str(db))
    _seed_ledger(db)
    old_day = (NOW - timedelta(days=8)).date().isoformat()
    _write_day(tmp_path, old_day, [_entry(1, status="needs_human")])
    update_entry(old_day, "job-1",
                 lambda e: e.update(error="no-confirmation: submission may have succeeded"),
                 queue_dir=tmp_path)
    _write_day(tmp_path, TODAY, [])

    run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)

    assert _ledger_owns(db) is True


def test_a_handoff_he_never_ticked_drops_off_after_seven_days(tmp_path):
    """Teodor, 2026-09-24: drop unticked LinkedIn handoffs after 7 days --
    postings go stale and a growing backlog makes the list useless."""
    old_day = (NOW - timedelta(days=8)).date().isoformat()
    stale = (NOW - timedelta(days=7, hours=1)).isoformat()
    fresh = (NOW - timedelta(days=2)).isoformat()
    _write_day(tmp_path, old_day, [
        _entry(1, status="handed_off", status_changed_at=stale),
        _entry(2, status="handed_off", status_changed_at=fresh),
        _entry(3, status="applied_by_hand", status_changed_at=stale),
    ])
    _write_day(tmp_path, TODAY, [])

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)

    assert [e["id"] for _, e in report.expired] == ["job-1"]
    q = _read(tmp_path, old_day)
    assert q["job-1"]["error"] == "auto_expired"
    assert q["job-2"]["status"] == "handed_off"
    assert q["job-3"]["status"] == "applied_by_hand"
