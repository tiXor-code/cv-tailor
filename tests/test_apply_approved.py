"""scripts/apply_approved.py -- the post-approval orchestrator state machine.

Loaded as a standalone script module (like scripts/tailor.py's test) since it
lives in scripts/, not the cv_tailor package. assemble_package, send_application,
crm_mark_applied, send_text, and send_document are monkeypatched directly on the
loaded module object -- no Azure/SMTP/Sheets/Telegram calls are ever made here.
update_entry is spied (real writes, call recorded) so tests can assert the exact
on-disk status trail without re-implementing the state machine.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "apply_approved.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("apply_approved_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _entry(**overrides):
    base = {
        "id": "job-1",
        "title": "AI Engineer",
        "company": "Acme Inc.",
        "location": "Remote",
        "url": "https://acme.example/jobs/1",
        "source": "lever",
        "apply_method": "email",
        "apply_target": "jobs@acme.example",
        "status": "approved",
        "package_dir": None,
        "cv_path": None,
        "cover_letter_path": None,
        "decided_at": None,
    }
    base.update(overrides)
    return base


def _write_queue(queue_dir, scan_date, entry):
    day_dir = queue_dir / scan_date
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "jobs.json").write_text(json.dumps([entry], indent=2))


def _read_entry(queue_dir, scan_date, job_id):
    entries = json.loads((queue_dir / scan_date / "jobs.json").read_text())
    return next(e for e in entries if e["id"] == job_id)


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(ROOT / "tests" / "fixtures" / "profile_minimal.yaml"))
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "jobs.db"))
    # Deterministic default regardless of this machine's real repo .env --
    # _load_dotenv's os.environ.setdefault(...) would otherwise pick up
    # whatever APPLY_ARMED the real .env holds the first time a test runs
    # without setting it explicitly, and (being a raw os.environ write, not
    # a monkeypatch one) that leaks into every later test in the session.
    # Tests that need armed=1 override this explicitly after the fixture runs.
    monkeypatch.setenv("APPLY_ARMED", "0")
    return _load_module()


def _spy_update_entry(mod, monkeypatch):
    """Wrap the real update_entry so writes still happen, and record the status
    trail (deduped consecutive repeats -- a paths-only write does not change
    status and should not appear as a new entry in the trail)."""
    real = mod.update_entry
    trail = []

    def spy(scan_date_iso, job_id, mutator, *, queue_dir=None, expect_status=None):
        result = real(scan_date_iso, job_id, mutator, queue_dir=queue_dir, expect_status=expect_status)
        status = result.get("status")
        if not trail or trail[-1] != status:
            trail.append(status)
        return result

    monkeypatch.setattr(mod, "update_entry", spy)
    return trail


class _FakeRunPortal:
    """Queued-reply stand-in for cv_tailor.portal.run_portal_application.
    Records every call's kwargs (profile/answers/client/deployment/dry_run/
    handoff/notify) so tests can assert the orchestrator threaded them
    through correctly, not just that it reacted to the returned status."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, entry, package, profile, answers, *, dry_run, client=None, deployment=None,
                 handoff=False, notify=None):
        self.calls.append({
            "entry": entry, "package": package, "profile": profile, "answers": answers,
            "dry_run": dry_run, "client": client, "deployment": deployment,
            "handoff": handoff, "notify": notify,
        })
        return self._results.pop(0)


def _fake_assemble(*, warnings=None, package_dir):
    warnings = warnings or []

    def fake(entry, scan_date, *, queue_dir=None, client=None):
        Path(package_dir).mkdir(parents=True, exist_ok=True)
        cv_path = str(Path(package_dir) / "cv.pdf")
        Path(cv_path).write_bytes(b"%PDF-1.4 fake\n")
        cover_path = str(Path(package_dir) / "cover_letter.md")
        Path(cover_path).write_text("Dear Hiring Manager,\n")
        return {
            "package_dir": str(package_dir), "cv_path": cv_path,
            "cover_letter_path": cover_path, "cover_letter_warnings": warnings,
            "slug": "2026-07-10-acme-inc-ai-engineer",
        }

    return fake


def test_happy_email_armed_goes_assembling_sending_sent(mod, monkeypatch, tmp_path):
    from cv_tailor.sender import SendResult

    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    calls = {"crm": [], "text": [], "doc": []}
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(
        mod, "send_application",
        lambda *a, **kw: SendResult(status="sent", recipient="jobs@acme.example", reason=""),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: calls["crm"].append(a) or True)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: calls["text"].append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: calls["doc"].append(a) or True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "sending", "sent"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "sent"
    assert entry.get("applied_at")
    assert entry["package_dir"] == str(tmp_path / "pkg")
    assert calls["crm"] == [("Acme Inc.", "AI Engineer", "https://acme.example/jobs/1")]
    assert len(calls["text"]) == 1
    assert len(calls["doc"]) == 1


def test_preview_sent_does_not_call_crm(mod, monkeypatch, tmp_path):
    from cv_tailor.sender import SendResult

    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    crm_calls = []
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(
        mod, "send_application",
        lambda *a, **kw: SendResult(status="preview_sent", recipient="contact@teodorlutoiu.com", reason=""),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: crm_calls.append(a) or True)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "sending", "preview_sent"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "preview_sent"
    assert entry.get("applied_at") is None
    assert crm_calls == []


def test_send_blocked_marks_failed_with_reason(mod, monkeypatch, tmp_path):
    from cv_tailor.sender import SendResult

    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(
        mod, "send_application",
        lambda *a, **kw: SendResult(status="blocked", recipient="", reason="duplicate"),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "sending", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert entry["error"] == "duplicate"


def test_cover_letter_warnings_no_longer_park_and_the_letter_is_telegrammed(
        mod, monkeypatch, tmp_path):
    """Teodor's decision, 2026-09-17: apply anyway, and send him the letter.

    Parking here was the LAST hard human gate in the pipeline. Nothing in
    autopilot ever advanced a needs_review entry -- it could only expire -- so
    Cohere (score 8) and Mistral.ai (7) sat in it indefinitely after clearing
    every other blocker. The warning is still surfaced, but as a notification
    after the fact rather than a wall in front of the application.

    CHANGED BEHAVIOUR: this test previously asserted the opposite
    (test_warnings_stop_at_needs_review_no_send). The requirement changed by
    explicit decision, not because the assertion was inconvenient."""
    from cv_tailor.sender import SendResult

    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(
        mod, "assemble_package",
        _fake_assemble(package_dir=tmp_path / "pkg", warnings=["banned phrase: 'leverage'"]),
    )
    sent = []
    monkeypatch.setattr(
        mod, "send_application",
        lambda *a, **kw: sent.append(1) or SendResult(
            status="sent", recipient="jobs@acme.example", reason=""),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: True)
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert "needs_review" not in trail, "the gate must no longer park the job"
    assert sent, "the application must actually go out"
    blob = " ".join(str(t) for t in texts)
    assert "leverage" in blob, "the warning must still be surfaced to Teodor"


def test_force_from_needs_review_skips_warnings_stop_and_sends(mod, monkeypatch, tmp_path):
    from cv_tailor.sender import SendResult

    _write_queue(tmp_path, "2026-07-10", _entry(status="needs_review"))
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(
        mod, "assemble_package",
        _fake_assemble(package_dir=tmp_path / "pkg", warnings=["banned phrase: 'leverage'"]),
    )
    sent = []
    monkeypatch.setattr(
        mod, "send_application",
        lambda *a, **kw: sent.append(1) or SendResult(status="sent", recipient="jobs@acme.example", reason=""),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1", "--force"])

    assert rc == 0
    assert trail == ["assembling", "sending", "sent"]
    assert sent == [1]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "sent"


def _portal_queue(tmp_path, **overrides):
    _write_queue(tmp_path, "2026-07-10", _entry(
        apply_method="portal", apply_target="https://acme.example/jobs/1", **overrides))


def _stub_portal_prereqs(mod, monkeypatch, *, answers=None, client=None):
    """Common portal-path stubs every trail test needs: assemble, a
    controlled answers.yaml, and a fake Azure client (never touches real
    creds -- build_azure_client() would raise without them)."""
    monkeypatch.setattr(mod, "load_answers", lambda *a, **kw: answers if answers is not None else {})
    monkeypatch.setattr(mod, "build_azure_client", lambda: client if client is not None else object())


# --- unarmed: dry-run preview only, ledger never touched --------------------

def test_portal_unarmed_filled_goes_ready_with_evidence(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    fake_run = _FakeRunPortal([PortalResult(status="filled", reason="", evidence_dir=evidence_dir)])
    monkeypatch.setattr(mod, "run_portal_application", fake_run)
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send email"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "ready"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "ready"
    assert entry["evidence_dir"] == evidence_dir
    assert len(texts) == 1
    assert fake_run.calls[0]["dry_run"] is True


def test_portal_unarmed_needs_human_no_send(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="needs_human", reason="captcha", evidence_dir=evidence_dir)]),
    )
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "needs_human"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "needs_human"
    assert entry["error"] == "captcha"
    assert entry["evidence_dir"] == evidence_dir
    assert len(texts) == 1


def test_portal_unarmed_failed_returns_1(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="failed", reason="TimeoutError: nav timeout", evidence_dir=evidence_dir)]),
    )
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert "nav timeout" in entry["error"]
    assert entry["evidence_dir"] == evidence_dir


def test_portal_unarmed_threads_profile_answers_and_client_into_run_portal_application(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    fake_answers = {"work_authorization": "EU citizen"}
    sentinel_client = object()
    _stub_portal_prereqs(mod, monkeypatch, answers=fake_answers, client=sentinel_client)
    fake_run = _FakeRunPortal(
        [PortalResult(status="filled", reason="", evidence_dir=str(tmp_path / "pkg" / "portal"))])
    monkeypatch.setattr(mod, "run_portal_application", fake_run)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    call = fake_run.calls[0]
    assert call["answers"] == fake_answers
    assert call["client"] is sentinel_client
    assert call["profile"]["contact"]["name"] == "Test User"  # tests/fixtures/profile_minimal.yaml


# --- armed: ledger gates, record-then-submit ---------------------------------

def test_portal_armed_submitted_goes_sending_sent_and_records_ledger(mod, monkeypatch, tmp_path):
    from cv_tailor.cache import application_exists, connect
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    fake_run = _FakeRunPortal([PortalResult(status="submitted", reason="", evidence_dir=evidence_dir)])
    monkeypatch.setattr(mod, "run_portal_application", fake_run)
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send email"))
    crm_calls = []
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: crm_calls.append(a) or True)
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "sending", "sent"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "sent"
    assert entry.get("applied_at")
    assert entry["evidence_dir"] == evidence_dir
    assert fake_run.calls[0]["dry_run"] is False
    assert crm_calls == [("Acme Inc.", "AI Engineer", "https://acme.example/jobs/1")]
    assert len(texts) == 1

    conn = connect(tmp_path / "jobs.db")
    assert application_exists(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer") is True


def test_portal_armed_needs_human_keeps_ledger_row(mod, monkeypatch, tmp_path):
    """Ambiguous outcome: the submission MAY have gone through -- the ledger
    row recorded before the attempt must survive, so the job is never
    silently re-submitted, and crm_mark_applied must NOT fire (it isn't
    confirmed sent)."""
    from cv_tailor.cache import application_exists, connect
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="needs_human", reason="no-confirmation", evidence_dir=evidence_dir)]),
    )
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called on an ambiguous outcome"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "sending", "needs_human"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "needs_human"
    assert entry["error"] == "no-confirmation"
    assert entry["evidence_dir"] == evidence_dir
    assert len(texts) == 1

    conn = connect(tmp_path / "jobs.db")
    assert application_exists(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer") is True


@pytest.mark.parametrize("reason", [
    "captcha", "login-required", "no-adapter", "missing-apply-target",
    "handoff-timeout: captcha not solved",
    # The resume upload is verified BEFORE any field is typed and long
    # before any submit click, in every adapter -- so this family proves no
    # submission happened just as surely as a captcha wall does. It arrives
    # in two shapes: bare (greenhouse) and with a diagnostic suffix naming
    # what was checked (ashby), so exact-match membership cannot catch it.
    #
    # Left un-rolled-back, it is what permanently blocked Flip GmbH
    # (2026-09-10), Checkly (09-12) and Sardine (09-16): each kept a ledger
    # row for an application that was never sent, and norm_key then blocked
    # every sibling role at those companies forever.
    "resume-upload-failed",
    "resume-upload-failed: no file input found (#_systemfield_resume and "
    "input[type='file'] fallback both matched 0 elements)",
    # A required question with no grounded answer aborts BEFORE the submit
    # call in every adapter (ashby returns at 508/517 with submit at 224;
    # greenhouse 393 vs 415; lever 270 vs 419), so nothing was ever sent.
    #
    # Live 2026-09-16: pragmatike (score 8, ashby) was discovered, scored,
    # approved and filled by autopilot on its own, then stopped at "Total
    # years of experience" -- correctly, because that is a factual claim and
    # guessing it on a real application is worse than parking. Its ledger row
    # survived anyway, which would block every pragmatike role forever for an
    # application that provably never left the machine.
    "unanswerable-required:Total years of experience",
])
def test_portal_armed_needs_human_presubmit_rolls_back_ledger(mod, monkeypatch, tmp_path, reason):
    """The 2026-07-10 phantom-row cascade: these reasons PROVE no submission
    happened (blocker checks run before any field is filled; no-adapter returns
    before a browser launches), yet the pre-recorded row marked the job as
    applied forever, blocked every same-company|role sibling as 'duplicate',
    and burned 6 of 10 daily-cap slots on walls. The row must be rolled back."""
    from cv_tailor.cache import application_exists, connect
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="needs_human", reason=reason, evidence_dir=evidence_dir)]),
    )
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    # Since 2026-09-25 a provable wall goes on his apply-yourself list;
    # the ledger rollback below is the invariant this test guards.
    want = "handed_off" if mod._handoff_reason(reason) else "needs_human"
    assert entry["status"] == want and entry["error"] == reason
    conn = connect(tmp_path / "jobs.db")
    # the row is gone: the job can be retried, and a same-norm_key sibling
    # (regional variant of the same role) is no longer blocked as "duplicate"
    from cv_tailor.cache import application_exists as _exists
    assert _exists(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer") is False


def test_portal_armed_failed_rolls_back_ledger_row(mod, monkeypatch, tmp_path):
    from cv_tailor.cache import application_exists, connect
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="failed", reason="RuntimeError: browser crashed", evidence_dir=evidence_dir)]),
    )
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "sending", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert "browser crashed" in entry["error"]
    assert entry["evidence_dir"] == evidence_dir
    assert len(texts) == 1  # best-effort failure notification attempted

    conn = connect(tmp_path / "jobs.db")
    assert application_exists(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer") is False


def test_portal_armed_duplicate_blocks_before_browser_attempt(mod, monkeypatch, tmp_path):
    from cv_tailor.cache import connect, record_application

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)

    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer",
                        url="https://acme.example/jobs/1", channel="portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        lambda *a, **kw: pytest.fail("must not attempt the browser on a duplicate"),
    )
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert entry["error"] == "duplicate"


def test_portal_armed_daily_cap_blocks_before_browser_attempt(mod, monkeypatch, tmp_path):
    from cv_tailor.cache import connect, record_application

    monkeypatch.setenv("APPLY_ARMED", "1")
    monkeypatch.setenv("APPLY_DAILY_CAP", "1")
    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)

    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="other-job", company="Other Co", role="Other Role",
                        url="https://other.example", channel="email")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        lambda *a, **kw: pytest.fail("must not attempt the browser over the daily cap"),
    )
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert entry["error"] == "daily-cap"


# --- --handoff: human-assisted completion, runs regardless of APPLY_ARMED ----

def test_handoff_from_needs_human_with_own_ledger_row_proceeds_and_sends(mod, monkeypatch, tmp_path):
    """A prior armed attempt hit needs_human and kept its ledger row for
    THIS job_id. --handoff resumes it: own_application_recorded is true, so
    the record step must proceed straight to the browser without another
    INSERT (own_row_skip) -- record_application must never even be called."""
    from cv_tailor.cache import connect, record_application
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path, status="needs_human")
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer",
                        url="https://acme.example/jobs/1", channel="portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    fake_run = _FakeRunPortal([PortalResult(status="submitted", reason="", evidence_dir=evidence_dir)])
    monkeypatch.setattr(mod, "run_portal_application", fake_run)
    monkeypatch.setattr(
        mod, "record_application",
        lambda *a, **kw: pytest.fail("own-row completion must not attempt a fresh insert"),
    )
    crm_calls = []
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: crm_calls.append(a) or True)
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)

    rc = mod.main(["2026-07-10", "job-1", "--handoff"])

    assert rc == 0
    assert trail == ["assembling", "sending", "sent"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "sent"
    assert entry["evidence_dir"] == evidence_dir
    assert crm_calls == [("Acme Inc.", "AI Engineer", "https://acme.example/jobs/1")]
    call = fake_run.calls[0]
    assert call["dry_run"] is False
    assert call["handoff"] is True
    assert call["notify"] is mod.send_text

    # still exactly one ledger row for job-1 -- no duplicate insert happened
    count = conn.execute("SELECT COUNT(*) FROM applications WHERE job_id='job-1'").fetchone()[0]
    assert count == 1


def test_handoff_different_jobs_same_company_role_row_blocks_duplicate(mod, monkeypatch, tmp_path):
    """own_application_recorded is false for job-1 (the existing row belongs
    to a DIFFERENT job_id) -- a same-company|role collision from another job
    must still block as a genuine duplicate, exactly like the non-handoff
    armed path."""
    from cv_tailor.cache import connect, record_application

    _portal_queue(tmp_path, status="needs_human")
    trail = _spy_update_entry(mod, monkeypatch)

    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="some-other-job-id", company="Acme Inc.", role="AI Engineer",
                        url="https://acme.example/jobs/999", channel="portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        lambda *a, **kw: pytest.fail("must not attempt the browser on a duplicate"),
    )
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1", "--handoff"])

    assert rc == 1
    assert trail == ["assembling", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert entry["error"] == "duplicate"


def test_handoff_timeout_result_marks_needs_human(mod, monkeypatch, tmp_path):
    """A fresh --handoff run (starting from `ready`, no prior ledger row)
    that times out waiting for the human to submit lands at needs_human with
    the adapter's handoff-timeout reason, same as any other needs_human."""
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path, status="ready")
    trail = _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(
            status="needs_human",
            reason="handoff-timeout: not submitted, form left as-is",
            evidence_dir=evidence_dir,
        )]),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)

    rc = mod.main(["2026-07-10", "job-1", "--handoff"])

    assert rc == 0
    assert trail == ["assembling", "sending", "needs_human"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "needs_human"
    assert entry["error"] == "handoff-timeout: not submitted, form left as-is"
    assert entry["evidence_dir"] == evidence_dir
    assert len(texts) == 1


def test_handoff_runs_regardless_of_apply_armed(mod, monkeypatch, tmp_path):
    """--handoff is human-authorized submission: it must reach the
    ledger-gated submit path even with APPLY_ARMED unset/0."""
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "0")
    _portal_queue(tmp_path, status="ready")
    _spy_update_entry(mod, monkeypatch)
    evidence_dir = str(tmp_path / "pkg" / "portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    fake_run = _FakeRunPortal([PortalResult(status="submitted", reason="", evidence_dir=evidence_dir)])
    monkeypatch.setattr(mod, "run_portal_application", fake_run)
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1", "--handoff"])

    assert rc == 0
    assert fake_run.calls[0]["dry_run"] is False  # never the unarmed dry-run branch
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "sent"


def test_portal_setup_raises_marks_failed_not_wedged_in_assembling(mod, monkeypatch, tmp_path):
    """Phase C fix: build_azure_client()/load_profile(strict=True)/
    load_answers() raising (missing env, malformed profile/answers) must
    not wedge the job at 'assembling' forever -- it must land as 'failed'
    with the error recorded, and a best-effort Telegram note must be
    attempted, mirroring the assemble/send exception guards."""
    _portal_queue(tmp_path)
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(mod, "load_answers", lambda *a, **kw: {})

    def boom():
        raise KeyError("AZURE_OPENAI_API_KEY")

    monkeypatch.setattr(mod, "build_azure_client", boom)
    monkeypatch.setattr(
        mod, "run_portal_application",
        lambda *a, **kw: pytest.fail("must not attempt the browser when setup failed"),
    )
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert "AZURE_OPENAI_API_KEY" in entry["error"]
    assert len(texts) == 1  # best-effort failure notification attempted


def test_portal_setup_raises_telegram_also_failing_still_returns_1(mod, monkeypatch, tmp_path):
    """The Telegram failure note is best-effort: if it too raises, the
    orchestrator must still report failure (not crash uncaught)."""
    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(mod, "load_answers", lambda *a, **kw: {})

    def boom():
        raise KeyError("AZURE_OPENAI_API_KEY")

    monkeypatch.setattr(mod, "build_azure_client", boom)

    def text_boom(*a, **kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(mod, "send_text", text_boom)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"


def test_assemble_raises_marks_failed_with_error(mod, monkeypatch, tmp_path):
    from cv_tailor.assemble import AssembleError

    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    def boom(*a, **kw):
        raise AssembleError("no JD text for job-1")

    monkeypatch.setattr(mod, "assemble_package", boom)
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert "no JD text" in entry["error"]


def test_wrong_start_status_exits_2_and_leaves_entry_untouched(mod, monkeypatch, tmp_path):
    _write_queue(tmp_path, "2026-07-10", _entry(status="pending"))
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", lambda *a, **kw: pytest.fail("must not assemble"))
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: pytest.fail("must not notify"))
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 2
    assert trail == []
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "pending"


def test_needs_review_without_force_is_a_wrong_start_status(mod, monkeypatch, tmp_path):
    _write_queue(tmp_path, "2026-07-10", _entry(status="needs_review"))
    trail = _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", lambda *a, **kw: pytest.fail("must not assemble"))

    rc = mod.main(["2026-07-10", "job-1"])  # no --force

    assert rc == 2
    assert trail == []


def test_send_application_raises_marks_failed_not_wedged_in_sending(mod, monkeypatch, tmp_path):
    """Finding 1: an SMTP exception must not leave the job wedged at
    'sending' forever -- it must land as 'failed' with the error recorded,
    and a best-effort Telegram note must be attempted."""
    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))

    def boom(*a, **kw):
        raise RuntimeError("smtp connection reset")

    monkeypatch.setattr(mod, "send_application", boom)
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: pytest.fail("must not be called"))

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    assert trail == ["assembling", "sending", "failed"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"
    assert "smtp connection reset" in entry["error"]
    assert len(texts) == 1  # best-effort failure notification attempted


def test_send_application_raises_telegram_also_failing_still_returns_1(mod, monkeypatch, tmp_path):
    """The Telegram failure note is best-effort: if it too raises, the
    orchestrator must still report failure (not crash uncaught)."""
    _write_queue(tmp_path, "2026-07-10", _entry())
    _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))

    def boom(*a, **kw):
        raise RuntimeError("smtp connection reset")

    monkeypatch.setattr(mod, "send_application", boom)

    def text_boom(*a, **kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(mod, "send_text", text_boom)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 1
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed"


def test_concurrent_double_spawn_second_gets_status_conflict(mod, monkeypatch, tmp_path):
    """Finding 2a: the first status transition is a compare-and-swap. If the
    entry's status has already moved off 'approved' by the time this process
    reaches the flock (simulated here by mutating the queue file directly,
    standing in for a concurrent winner), main() must exit 2 without
    clobbering the winner's state."""
    _write_queue(tmp_path, "2026-07-10", _entry())

    monkeypatch.setattr(mod, "assemble_package", lambda *a, **kw: pytest.fail("must not assemble"))

    # Simulate a concurrent winner: flip the on-disk status to 'assembling'
    # right before this process's own CAS write, by monkeypatching
    # update_entry to mutate the file out from under expect_status first.
    from cv_tailor.scout_queue import update_entry as real_update_entry_fn

    def racing_update_entry(scan_date_iso, job_id, mutator, *, queue_dir=None, expect_status=None):
        if expect_status == "approved":
            # A concurrent winner already claimed it.
            real_update_entry_fn(
                scan_date_iso, job_id, lambda e: e.update(status="assembling"),
                queue_dir=queue_dir,
            )
        return real_update_entry_fn(
            scan_date_iso, job_id, mutator, queue_dir=queue_dir, expect_status=expect_status
        )

    monkeypatch.setattr(mod, "update_entry", racing_update_entry)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 2
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "assembling"  # the "winner's" state, untouched by the loser


def test_load_dotenv_setdefault_semantics(tmp_path, monkeypatch):
    """The detached-spawn bootstrap loads .env but explicit environment wins."""
    import os
    mod = _load_module()
    envfile = tmp_path / ".env"
    envfile.write_text(
        "# comment\nAZURE_OPENAI_API_KEY=from-dotenv\nAPPLY_ARMED=1\n"
        'QUOTED="q-value"\nbroken-line-no-equals\n')
    monkeypatch.setenv("APPLY_ARMED", "0")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    mod._load_dotenv(envfile)
    assert os.environ["AZURE_OPENAI_API_KEY"] == "from-dotenv"
    assert os.environ["APPLY_ARMED"] == "0"  # explicit env wins over .env
    assert os.environ["QUOTED"] == "q-value"
    mod._load_dotenv(tmp_path / "missing.env")  # silent no-op


def test_portal_aggregator_target_gets_resolved_before_attempt(mod, monkeypatch, tmp_path):
    """An aggregator apply_target with no adapter is rewritten to the resolved
    ATS URL (original preserved) before the portal attempt runs."""
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "0")
    entry = _entry(apply_method="portal",
                   apply_target="https://remoteok.example/listing/1",
                   url="https://remoteok.example/listing/1")
    _write_queue(Path(mod.queue_root()), "2026-07-10", entry)
    _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url",
                        lambda e: "https://jobs.ashbyhq.com/acme/aaa-bbb")
    seen_targets = []

    def fake_portal(entry, meta, profile, answers, dry_run=True, client=None, **kw):
        seen_targets.append(entry.get("apply_target"))
        return PortalResult(status="filled", reason="", evidence_dir=str(tmp_path))

    monkeypatch.setattr(mod, "run_portal_application", fake_portal)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])
    assert rc == 0
    assert seen_targets == ["https://jobs.ashbyhq.com/acme/aaa-bbb"]
    e = _read_entry(Path(mod.queue_root()), "2026-07-10", "job-1")
    assert e["apply_target"] == "https://jobs.ashbyhq.com/acme/aaa-bbb"
    assert e["apply_target_original"] == "https://remoteok.example/listing/1"


def test_portal_adapter_target_skips_resolver(mod, monkeypatch, tmp_path):
    """A target an adapter already claims must NOT trigger any resolution."""
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "0")
    entry = _entry(apply_method="portal",
                   apply_target="https://jobs.ashbyhq.com/acme/existing",
                   url="https://jobs.ashbyhq.com/acme/existing")
    _write_queue(Path(mod.queue_root()), "2026-07-10", entry)
    _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url",
                        lambda e: pytest.fail("resolver must not run"))
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="filled", reason="", evidence_dir=str(tmp_path))]),
    )
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    assert mod.main(["2026-07-10", "job-1"]) == 0


# --- Task 6: the question that blocked the job, as a field ------------------

def test_parse_blocked_question_reads_both_required_question_reasons(mod):
    """The label is already in the reason string; it just was not reachable
    without substring-matching an error message."""
    assert mod.parse_blocked_question(
        "unanswerable-required:Current company") == ("Current company", "unanswerable")
    assert mod.parse_blocked_question(
        "unwritable-required:LinkedIn Profile") == ("LinkedIn Profile", "unwritable")


def test_parse_blocked_question_keeps_a_label_containing_a_colon(mod):
    """Only the first colon separates the prefix; real ATS labels contain
    colons and the whole question has to survive."""
    assert mod.parse_blocked_question(
        "unanswerable-required:Notice period: how long?"
    ) == ("Notice period: how long?", "unanswerable")


def test_parse_blocked_question_is_none_for_every_other_reason(mod):
    for reason in ("captcha", "login-required", "no-adapter", "no-confirmation",
                   "timeout", "handoff-timeout: captcha not solved",
                   "unanswerable-required", "", None,
                   "TimeoutError: unanswerable-required:not a prefix"):
        assert mod.parse_blocked_question(reason) is None, reason


def test_record_blocked_question_clears_a_previous_attempts_question(mod):
    """A second attempt that dies on a captcha must not still be tagged with
    the question the FIRST attempt could not answer."""
    e = {"blocked_question": "Current company", "blocked_question_kind": "unanswerable"}
    mod._record_blocked_question(e, "captcha")
    assert "blocked_question" not in e and "blocked_question_kind" not in e


def test_portal_unarmed_needs_human_records_the_blocking_question(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    # offline + deterministic: the aggregator resolver would otherwise try
    # to fetch the fake apply_target on every run of these tests
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="needs_human",
                                     reason="unanswerable-required:Years of Python",
                                     evidence_dir=str(tmp_path))]),
    )
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 0

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["error"] == "unanswerable-required:Years of Python"  # unchanged
    assert entry["blocked_question"] == "Years of Python"
    assert entry["blocked_question_kind"] == "unanswerable"


def test_portal_armed_needs_human_records_the_blocking_question(mod, monkeypatch, tmp_path):
    """The armed path is where this data actually accrues (APPLY_ARMED=1 in
    prod), and it is the one the brief names."""
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    # offline + deterministic: the aggregator resolver would otherwise try
    # to fetch the fake apply_target on every run of these tests
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="needs_human",
                                     reason="unwritable-required:Work Authorization",
                                     evidence_dir=str(tmp_path))]),
    )
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("not confirmed sent"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 0

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "needs_human"
    assert entry["blocked_question"] == "Work Authorization"
    # kept distinct from unanswerable: an answer EXISTED and the form refused
    # it, so adding one more answer to answers.yaml would not fix this job
    assert entry["blocked_question_kind"] == "unwritable"


def test_portal_needs_human_on_a_wall_records_no_question(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    # offline + deterministic: the aggregator resolver would otherwise try
    # to fetch the fake apply_target on every run of these tests
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(
        mod, "run_portal_application",
        _FakeRunPortal([PortalResult(status="needs_human", reason="captcha",
                                     evidence_dir=str(tmp_path))]),
    )
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 0

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["error"] == "captcha"
    assert "blocked_question" not in entry


def test_a_later_success_clears_the_earlier_blocking_question(mod, monkeypatch, tmp_path):
    """Park on an unanswerable question, add the answer, retry, succeed -- the
    exact workflow blocked_question_kind describes. update_entry mutates the
    entry in place, so a success that does not clear the field leaves the job
    counted as still blocked forever, corrupting the one metric this data
    exists to produce. These keys always describe the LATEST attempt."""
    from cv_tailor.portal import PortalResult

    _portal_queue(tmp_path)
    _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(mod, "run_portal_application", _FakeRunPortal([
        PortalResult(status="needs_human", reason="unanswerable-required:Years of Python",
                     evidence_dir=str(tmp_path)),
        PortalResult(status="filled", reason="", evidence_dir=str(tmp_path)),
    ]))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    # attempt 1: parked on the question
    assert mod.main(["2026-07-10", "job-1"]) == 0
    parked = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert parked["blocked_question"] == "Years of Python"

    # the answer gets added and the job is re-approved for another attempt
    mod.update_entry("2026-07-10", "job-1", lambda e: e.update(status="approved"))

    # attempt 2: the form fills cleanly
    assert mod.main(["2026-07-10", "job-1"]) == 0

    done = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert done["status"] == "ready"
    assert "blocked_question" not in done, "a successful attempt still reads as blocked"
    assert "blocked_question_kind" not in done


def test_armed_submit_clears_a_stale_blocking_question(mod, monkeypatch, tmp_path):
    """Same invariant on the armed success mutator -- the one that runs in
    production. The entry arrives carrying a question from an earlier attempt;
    a real submit must not leave it behind."""
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path, blocked_question="Years of Python",
                  blocked_question_kind="unanswerable")
    _spy_update_entry(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(mod, "run_portal_application", _FakeRunPortal(
        [PortalResult(status="submitted", reason="", evidence_dir=str(tmp_path))]))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 0

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "sent"
    assert "blocked_question" not in entry and "blocked_question_kind" not in entry


# --- the blocked-question invariant holds on the non-portal writes too -------
#
# The portal outcome mutators all call _record_blocked_question, success
# included, so the fields always describe the LATEST attempt. The ledger-gate
# refusals ("duplicate", "daily-cap"), the mid-flight "sending" write and the
# email-track writes did not, so a job that parked on an unanswerable required
# question and then hit one of those on a later attempt ended up with a stale
# question sitting beside an unrelated error -- which corrupts the one metric
# these fields exist to produce.

def _spy_question_trail(mod, monkeypatch):
    """Record (status, blocked_question) after EVERY write, so the invariant can
    be asserted at each intermediate state and not just at the end."""
    real = mod.update_entry
    trail = []

    def spy(scan_date_iso, job_id, mutator, *, queue_dir=None, expect_status=None):
        result = real(scan_date_iso, job_id, mutator, queue_dir=queue_dir,
                      expect_status=expect_status)
        trail.append((result.get("status"), result.get("blocked_question")))
        return result

    monkeypatch.setattr(mod, "update_entry", spy)
    return trail


def test_duplicate_refusal_clears_a_stale_blocking_question(mod, monkeypatch, tmp_path):
    from cv_tailor.cache import connect, record_application

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path, blocked_question="Years of Python",
                  blocked_question_kind="unanswerable")
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer",
                        url="https://acme.example/jobs/1", channel="portal")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(mod, "run_portal_application",
                        lambda *a, **kw: pytest.fail("must not attempt the browser"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 1

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed" and entry["error"] == "duplicate"
    assert "blocked_question" not in entry and "blocked_question_kind" not in entry


def test_daily_cap_refusal_clears_a_stale_blocking_question(mod, monkeypatch, tmp_path):
    from cv_tailor.cache import connect, record_application

    monkeypatch.setenv("APPLY_ARMED", "1")
    monkeypatch.setenv("APPLY_DAILY_CAP", "1")
    _portal_queue(tmp_path, blocked_question="Work Authorization",
                  blocked_question_kind="unwritable")
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="other-job", company="Other Co", role="Other Role",
                        url="https://other.example", channel="email")

    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(mod, "run_portal_application",
                        lambda *a, **kw: pytest.fail("must not attempt the browser"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 1

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed" and entry["error"] == "daily-cap"
    assert "blocked_question" not in entry and "blocked_question_kind" not in entry


def test_no_intermediate_write_carries_the_previous_attempts_question(mod, monkeypatch, tmp_path):
    """The stale question must be gone from the FIRST write of the new attempt,
    not just from its terminal one -- a crash mid-attempt would otherwise leave
    the entry parked with a question that belongs to the previous run."""
    from cv_tailor.portal import PortalResult

    monkeypatch.setenv("APPLY_ARMED", "1")
    _portal_queue(tmp_path, blocked_question="Years of Python",
                  blocked_question_kind="unanswerable")
    trail = _spy_question_trail(mod, monkeypatch)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    _stub_portal_prereqs(mod, monkeypatch)
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    monkeypatch.setattr(mod, "run_portal_application", _FakeRunPortal(
        [PortalResult(status="needs_human", reason="unwritable-required:Notice period",
                      evidence_dir=str(tmp_path))]))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("not sent"))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 0

    assert ("assembling", None) in trail
    assert ("sending", None) in trail
    assert trail[-1] == ("needs_human", "Notice period")
    assert not any(q == "Years of Python" for _, q in trail), \
        f"a write still carried the previous attempt's question: {trail}"


def test_email_send_failure_clears_a_stale_blocking_question(mod, monkeypatch, tmp_path):
    """The email track never sets a blocked question, so it must not preserve
    one either: the same entry can reach it after a portal attempt parked."""
    _write_queue(tmp_path, "2026-07-10", _entry(blocked_question="Years of Python",
                                                blocked_question_kind="unanswerable"))
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(mod, "send_application",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("smtp down")))
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    assert mod.main(["2026-07-10", "job-1"]) == 1

    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "failed" and "smtp down" in entry["error"]
    assert "blocked_question" not in entry and "blocked_question_kind" not in entry


# --- supervised LinkedIn Easy Apply (Teodor, 2026-09-24: option B, 10/day) ----

def _linkedin_entry(**overrides):
    base = dict(source="linkedin", apply_method="portal", score=8, why="Strong fit.",
                url="https://www.linkedin.com/jobs/view/4457351096",
                apply_target="https://www.linkedin.com/jobs/view/4457351096")
    base.update(overrides)
    return _entry(**base)


def _handoff_setup(mod, monkeypatch, tmp_path, *, armed="1"):
    monkeypatch.setenv("APPLY_ARMED", armed)
    monkeypatch.setattr(mod, "assemble_package", _fake_assemble(package_dir=tmp_path / "pkg"))
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    # never the real answers.yaml inside a test
    monkeypatch.setattr(mod, "load_answers", lambda *a, **k: {"years_experience": 9})
    monkeypatch.setattr(mod, "build_azure_client", lambda *a, **k: None)
    portal = _FakeRunPortal([])
    monkeypatch.setattr(mod, "run_portal_application", portal)
    texts, docs = [], []
    monkeypatch.setattr(mod, "send_text", lambda t, **k: texts.append(t) or True)
    monkeypatch.setattr(mod, "send_document",
                        lambda p, caption="", **k: docs.append(str(p)) or True)
    return portal, texts, docs


def test_a_linkedin_job_with_no_form_is_handed_to_teodor(mod, monkeypatch, tmp_path):
    """~92% of LinkedIn rows lead to no form Scout can fill. Instead of parking
    no-adapter silently, send him everything he needs to apply himself -- and
    no browser ever touches LinkedIn."""
    _write_queue(tmp_path, "2026-09-24", _linkedin_entry())
    portal, texts, docs = _handoff_setup(mod, monkeypatch, tmp_path)

    rc = mod.main(["2026-09-24", "job-1"])

    assert rc == 0
    entry = _read_entry(tmp_path, "2026-09-24", "job-1")
    assert entry["status"] == "handed_off"
    assert entry.get("handed_off_at")
    assert portal.calls == [], "no browser may ever open LinkedIn"
    # The admin /scout list shows it; the autopilot digest is the one summary
    # message (Teodor, 2026-09-24). No per-job Telegram card or attachments.
    assert texts == [] and docs == []
    assert "Years of experience: 9" in entry["answer_sheet"]


def test_a_job_he_already_applied_to_is_not_handed_off_again(mod, monkeypatch, tmp_path):
    """Same company|role already in the ledger (sent by Scout, or ticked by him
    from an earlier posting): handing it off again would ask him to apply twice."""
    _write_queue(tmp_path, "2026-09-24", _linkedin_entry())
    _handoff_setup(mod, monkeypatch, tmp_path)
    conn = mod.connect(tmp_path / "jobs.db")
    mod.record_application(conn, job_id="older-posting", company="Acme Inc.",
                           role="AI Engineer", url="https://x.example", channel="portal")

    mod.main(["2026-09-24", "job-1"])

    entry = _read_entry(tmp_path, "2026-09-24", "job-1")
    assert entry["status"] == "failed" and entry["error"] == "duplicate"


def test_a_handoff_is_never_recorded_as_applied(mod, monkeypatch, tmp_path):
    """Only Teodor knows whether he clicked Submit. A ledger row would claim an
    application that may never happen and block the company via norm_key."""
    _write_queue(tmp_path, "2026-09-24", _linkedin_entry())
    _handoff_setup(mod, monkeypatch, tmp_path)

    mod.main(["2026-09-24", "job-1"])

    conn = mod.connect(tmp_path / "jobs.db")
    assert not mod.own_application_recorded(conn, "job-1")


def test_at_the_daily_cap_the_job_waits_for_tomorrow(mod, monkeypatch, tmp_path):
    """10 a day, his number. Over the cap nothing is sent and the job goes back
    to pending, so tomorrow's autopilot (highest score first) picks it up."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    other = tmp_path / "2026-09-23"
    other.mkdir(parents=True)
    (other / "jobs.json").write_text(json.dumps(
        [{"id": f"h{i}", "status": "handed_off", "handed_off_at": now} for i in range(10)]))
    _write_queue(tmp_path, "2026-09-24", _linkedin_entry())
    _, texts, docs = _handoff_setup(mod, monkeypatch, tmp_path)

    rc = mod.main(["2026-09-24", "job-1"])

    assert rc == 0
    assert _read_entry(tmp_path, "2026-09-24", "job-1")["status"] == "pending"
    assert texts == [] and docs == []


def test_an_unarmed_run_never_messages_him(mod, monkeypatch, tmp_path):
    """Dry runs and a paused system stay silent: the old dry-run path runs."""
    from cv_tailor.portal import PortalResult
    _write_queue(tmp_path, "2026-09-24", _linkedin_entry())
    portal, texts, _ = _handoff_setup(mod, monkeypatch, tmp_path, armed="0")
    portal._results = [PortalResult(status="needs_human", reason="no-adapter", evidence_dir="")]

    mod.main(["2026-09-24", "job-1"])

    assert not any("linkedin.com/jobs/view" in t for t in texts)
    assert _read_entry(tmp_path, "2026-09-24", "job-1")["status"] != "handed_off"


def _walled(mod, monkeypatch, tmp_path, reason, **entry):
    from cv_tailor.portal import PortalResult
    _write_queue(tmp_path, "2026-09-25", _linkedin_entry(
        source="ashby", url="https://jobs.ashbyhq.com/fixture/1",
        apply_target="https://jobs.ashbyhq.com/fixture/1", **entry))
    portal, texts, docs = _handoff_setup(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "resolve_ats_url", lambda e: None)
    portal._results = [PortalResult(status="needs_human", reason=reason, evidence_dir="")]
    mod.main(["2026-09-25", "job-1"])
    return _read_entry(tmp_path, "2026-09-25", "job-1"), portal, texts


# Teodor 2026-09-25 ("go for both a and b"): a job Scout provably could not
# submit goes on his apply-yourself list with everything ready, instead of a
# needs-human park he has to chase.

def test_a_spam_flagged_submit_goes_on_his_list(mod, monkeypatch, tmp_path):
    entry, portal, texts = _walled(mod, monkeypatch, tmp_path,
                                   "submit-rejected: the portal explicitly refused the submission")
    assert entry["status"] == "handed_off"
    assert entry.get("handed_off_at") and entry.get("answer_sheet")
    assert "spam" in entry["handoff_reason"].lower()
    assert not mod.own_application_recorded(mod.connect(tmp_path / "jobs.db"), "job-1")
    assert texts == [], "the digest is the one message; no per-job Telegram"


def test_captcha_and_no_adapter_go_on_his_list(mod, monkeypatch, tmp_path):
    entry, _, _ = _walled(mod, monkeypatch, tmp_path, "captcha")
    assert entry["status"] == "handed_off" and "captcha" in entry["handoff_reason"].lower()


def test_a_question_only_he_can_answer_goes_on_his_list_naming_it(mod, monkeypatch, tmp_path):
    entry, _, _ = _walled(mod, monkeypatch, tmp_path,
                          "unanswerable-required:Have you shipped a fixture to production?")
    assert entry["status"] == "handed_off"
    assert "Have you shipped a fixture to production?" in entry["handoff_reason"]


@pytest.mark.parametrize("reason", ["no-confirmation: submission may have gone through", "timeout"])
def test_an_ambiguous_outcome_is_never_handed_off(mod, monkeypatch, tmp_path, reason):
    """no-confirmation / timeout may have submitted: handing it off could make
    him apply twice. It stays a needs-human park."""
    entry, _, _ = _walled(mod, monkeypatch, tmp_path, reason)
    assert entry["status"] == "needs_human", reason


def test_a_non_linkedin_no_adapter_job_goes_on_his_list(mod, monkeypatch, tmp_path):
    from cv_tailor.portal import PortalResult
    _write_queue(tmp_path, "2026-09-24", _linkedin_entry(
        source="arbeitnow", url="https://www.arbeitnow.com/jobs/x",
        apply_target="https://www.arbeitnow.com/jobs/x"))
    portal, _, _ = _handoff_setup(mod, monkeypatch, tmp_path)
    portal._results = [PortalResult(status="needs_human", reason="no-adapter", evidence_dir="")]
    mod.main(["2026-09-24", "job-1"])
    entry = _read_entry(tmp_path, "2026-09-24", "job-1")
    assert entry["status"] == "handed_off"
    assert "no form" in entry["handoff_reason"].lower()


def test_a_refused_job_skips_the_daily_cap_it_must_never_be_retried(mod, monkeypatch, tmp_path):
    """Over the cap a LinkedIn job goes back to pending, which re-runs the
    automation tomorrow. For a spam-refused job that would resubmit into the
    same filter, so a refusal always lands on the list."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    other = tmp_path / "2026-09-24"; other.mkdir(parents=True)
    (other / "jobs.json").write_text(json.dumps(
        [{"id": f"h{i}", "status": "handed_off", "handed_off_at": now} for i in range(10)]))
    entry, _, _ = _walled(mod, monkeypatch, tmp_path, "submit-rejected: refused")
    assert entry["status"] == "handed_off"
