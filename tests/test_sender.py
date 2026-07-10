# tests/test_sender.py
"""Gated SMTP sender: armed flag, daily cap, dedupe ledger, preview mode.

All tests inject a fake smtp_factory -- no real network connection is ever
made from this test module.
"""
import email
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from cv_tailor.cache import connect, record_application
from cv_tailor.sender import SendResult, send_application


class FakeSMTP:
    """Records starttls/login/sendmail/quit calls. No network."""

    def __init__(self):
        self.starttls_calls = 0
        self.login_calls = []
        self.sendmail_calls = []
        self.quit_calls = 0

    def starttls(self, *args, **kwargs):
        self.starttls_calls += 1

    def login(self, user, password):
        self.login_calls.append((user, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.sendmail_calls.append((from_addr, to_addrs, msg))

    def quit(self):
        self.quit_calls += 1


def _profile():
    return {
        "contact": {
            "name": "Teodor-Cristian Lutoiu",
            "email": "contact@teodorlutoiu.com",
            "phone": "+40 725 697 859",
            "location": "Bucharest, Romania",
            "website": "teodorlutoiu.com",
            "linkedin": "linkedin.com/in/teodorlc",
            "github": "github.com/tiXor-code",
        }
    }


def _entry(**overrides):
    base = {
        "id": "job-1",
        "title": "AI Engineer",
        "company": "Acme Inc.",
        "url": "https://acme.example/jobs/1",
        "apply_method": "email",
        "apply_target": "jobs@acme.example",
    }
    base.update(overrides)
    return base


def _pkg_dir(tmp_path, cover_text="Dear Hiring Manager,\n\nI would love to join Acme.\n"):
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "cv.pdf").write_bytes(b"%PDF-1.4 fake pdf bytes\n")
    (pkg_dir / "cover_letter.md").write_text(cover_text, encoding="utf-8")
    return pkg_dir


def _set_env(monkeypatch, *, armed="0", cap="10"):
    monkeypatch.setenv("APPLY_SMTP_USER", "contact@teodorlutoiu.com")
    monkeypatch.setenv("APPLY_SMTP_PASSWORD", "fake-app-password")
    monkeypatch.setenv("APPLY_ARMED", armed)
    monkeypatch.setenv("APPLY_DAILY_CAP", cap)


def _sent_message(fake: FakeSMTP):
    assert len(fake.sendmail_calls) == 1
    _, _, raw = fake.sendmail_calls[0]
    return email.message_from_string(raw)


def test_unarmed_sends_preview_to_self_and_does_not_write_ledger(tmp_path, monkeypatch):
    _set_env(monkeypatch, armed="0")
    conn = connect(tmp_path / "jobs.db")
    pkg_dir = _pkg_dir(tmp_path)
    fake = FakeSMTP()

    result = send_application(
        _entry(), pkg_dir, _profile(), conn=conn, smtp_factory=lambda: fake
    )

    assert result == SendResult(
        status="preview_sent", recipient="contact@teodorlutoiu.com", reason=""
    )
    msg = _sent_message(fake)
    assert msg["Subject"].startswith("[PREVIEW] Application for")
    assert msg["To"] == "contact@teodorlutoiu.com"

    row = conn.execute("SELECT COUNT(*) FROM applications").fetchone()
    assert row[0] == 0


def test_armed_sends_to_apply_target_composes_and_records(tmp_path, monkeypatch):
    _set_env(monkeypatch, armed="1")
    conn = connect(tmp_path / "jobs.db")
    cover_text = "Dear Hiring Manager,\n\nI would love to join Acme.\n"
    pkg_dir = _pkg_dir(tmp_path, cover_text=cover_text)
    fake = FakeSMTP()

    result = send_application(
        _entry(), pkg_dir, _profile(), conn=conn, smtp_factory=lambda: fake
    )

    assert result == SendResult(
        status="sent", recipient="jobs@acme.example", reason=""
    )
    assert fake.login_calls == [("contact@teodorlutoiu.com", "fake-app-password")]
    assert fake.starttls_calls == 1

    msg = _sent_message(fake)
    assert msg["Subject"] == "Application for AI Engineer - Teodor-Cristian Lutoiu"
    assert msg["To"] == "jobs@acme.example"

    body = ""
    filenames = []
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            body += part.get_payload(decode=True).decode("utf-8")
        if part.get_filename():
            filenames.append(part.get_filename())

    assert "I would love to join Acme." in body
    contact = _profile()["contact"]
    for field in ("name", "phone", "email", "website", "linkedin", "github"):
        assert contact[field] in body
    assert filenames == ["Teodor-Lutoiu-CV-Acme-Inc.pdf"]

    from cv_tailor.cache import application_exists
    assert application_exists(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer")


def test_duplicate_blocks_before_smtp(tmp_path, monkeypatch):
    _set_env(monkeypatch, armed="1")
    conn = connect(tmp_path / "jobs.db")
    record_application(
        conn, job_id="job-1", company="Acme Inc.", role="AI Engineer",
        url="https://acme.example/jobs/1", channel="email",
    )
    pkg_dir = _pkg_dir(tmp_path)
    fake = FakeSMTP()

    result = send_application(
        _entry(), pkg_dir, _profile(), conn=conn, smtp_factory=lambda: fake
    )

    assert result == SendResult(status="blocked", recipient="", reason="duplicate")
    assert fake.sendmail_calls == []
    assert fake.login_calls == []


def test_daily_cap_blocks_armed_send(tmp_path, monkeypatch):
    _set_env(monkeypatch, armed="1", cap="1")
    conn = connect(tmp_path / "jobs.db")
    record_application(
        conn, job_id="other-job", company="Other Co", role="Other Role",
        url="https://other.example/jobs/9", channel="email",
    )
    pkg_dir = _pkg_dir(tmp_path)
    fake = FakeSMTP()

    result = send_application(
        _entry(), pkg_dir, _profile(), conn=conn, smtp_factory=lambda: fake
    )

    assert result == SendResult(status="blocked", recipient="", reason="daily-cap")
    assert fake.sendmail_calls == []


def test_unarmed_preview_is_not_subject_to_daily_cap(tmp_path, monkeypatch):
    _set_env(monkeypatch, armed="0", cap="1")
    conn = connect(tmp_path / "jobs.db")
    record_application(
        conn, job_id="other-job", company="Other Co", role="Other Role",
        url="https://other.example/jobs/9", channel="email",
    )
    pkg_dir = _pkg_dir(tmp_path)
    fake = FakeSMTP()

    result = send_application(
        _entry(), pkg_dir, _profile(), conn=conn, smtp_factory=lambda: fake
    )

    assert result.status == "preview_sent"
    assert len(fake.sendmail_calls) == 1


def test_missing_apply_target_blocks_with_reason(tmp_path, monkeypatch):
    _set_env(monkeypatch, armed="1")
    conn = connect(tmp_path / "jobs.db")
    pkg_dir = _pkg_dir(tmp_path)
    fake = FakeSMTP()

    result = send_application(
        _entry(apply_target=""), pkg_dir, _profile(), conn=conn, smtp_factory=lambda: fake
    )

    assert result.status == "blocked"
    assert "apply_target" in result.reason
    assert fake.sendmail_calls == []
