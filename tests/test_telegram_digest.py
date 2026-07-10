from types import SimpleNamespace
from cv_tailor.telegram import format_digest_for_telegram, send_document


def _item():
    job = SimpleNamespace(org="Acme", title="AI Engineer", location="Remote", url="https://j/1")
    return {"job": job, "score": 9, "reason": "fit"}


def test_digest_points_to_admin_not_cli():
    out = format_digest_for_telegram([_item()], "2026-06-24")
    assert "https://admin.teodorlutoiu.com/scout" in out
    assert "process_approved.py" not in out


def test_empty_digest_unchanged():
    assert "0 new candidates" in format_digest_for_telegram([], "2026-06-24")


def test_digest_tile_includes_each_job_url():
    job1 = SimpleNamespace(org="Acme", title="AI Engineer", location="Remote", url="https://acme.example/jobs/1")
    job2 = SimpleNamespace(org="Beta", title="ML Engineer", location="Remote", url="https://beta.example/jobs/2")
    scored = [
        {"job": job1, "score": 9, "reason": "fit"},
        {"job": job2, "score": 8, "reason": "also fit"},
    ]
    out = format_digest_for_telegram(scored, "2026-07-10")
    assert "https://acme.example/jobs/1" in out
    assert "https://beta.example/jobs/2" in out
    # each tile carries its OWN job's url, not just the footer link
    tile1, tile2 = out.split("2. ")
    assert "https://acme.example/jobs/1" in tile1
    assert "https://beta.example/jobs/2" in tile2
    assert "https://acme.example/jobs/1" not in tile2


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_send_document_posts_multipart_and_returns_true(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"%PDF-1.4 fake pdf bytes")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["content_type"] = req.get_header("Content-type")
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("cv_tailor.telegram.urllib.request.urlopen", fake_urlopen)

    assert send_document(str(doc), "here is my cv", chat_id=None, token=None) is True
    assert captured["url"].endswith("/sendDocument")
    assert b"%PDF-1.4 fake pdf bytes" in captured["body"]
    assert b"42" in captured["body"]
    assert captured["content_type"].startswith("multipart/form-data")


def test_send_document_failure_returns_false_not_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"data")

    def fake_urlopen(req, timeout=30):
        raise OSError("network down")

    monkeypatch.setattr("cv_tailor.telegram.urllib.request.urlopen", fake_urlopen)

    assert send_document(str(doc)) is False


def test_send_document_missing_credentials_returns_false(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"data")

    assert send_document(str(doc)) is False
