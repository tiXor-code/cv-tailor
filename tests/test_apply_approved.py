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
    Records every call's kwargs (profile/answers/client/deployment/dry_run)
    so tests can assert the orchestrator threaded them through correctly,
    not just that it reacted to the returned status."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, entry, package, profile, answers, *, dry_run, client=None, deployment=None):
        self.calls.append({
            "entry": entry, "package": package, "profile": profile, "answers": answers,
            "dry_run": dry_run, "client": client, "deployment": deployment,
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


def test_warnings_stop_at_needs_review_no_send(mod, monkeypatch, tmp_path):
    _write_queue(tmp_path, "2026-07-10", _entry())
    trail = _spy_update_entry(mod, monkeypatch)

    monkeypatch.setattr(
        mod, "assemble_package",
        _fake_assemble(package_dir=tmp_path / "pkg", warnings=["banned phrase: 'leverage'"]),
    )
    monkeypatch.setattr(mod, "send_application", lambda *a, **kw: pytest.fail("must not send"))
    monkeypatch.setattr(mod, "crm_mark_applied", lambda *a, **kw: pytest.fail("must not be called"))
    texts = []
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: texts.append(a) or True)
    monkeypatch.setattr(mod, "send_document", lambda *a, **kw: True)

    rc = mod.main(["2026-07-10", "job-1"])

    assert rc == 0
    assert trail == ["assembling", "needs_review"]
    entry = _read_entry(tmp_path, "2026-07-10", "job-1")
    assert entry["status"] == "needs_review"
    assert entry["warnings"] == ["banned phrase: 'leverage'"]
    assert len(texts) == 1


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


def _portal_queue(tmp_path):
    _write_queue(tmp_path, "2026-07-10", _entry(
        apply_method="portal", apply_target="https://acme.example/jobs/1"))


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
