from types import SimpleNamespace
from cv_tailor.telegram import format_digest_for_telegram


def _item():
    job = SimpleNamespace(org="Acme", title="AI Engineer", location="Remote", url="https://j/1")
    return {"job": job, "score": 9, "reason": "fit"}


def test_digest_points_to_admin_not_cli():
    out = format_digest_for_telegram([_item()], "2026-06-24")
    assert "https://admin.teodorlutoiu.com/scout" in out
    assert "process_approved.py" not in out


def test_empty_digest_unchanged():
    assert "0 new candidates" in format_digest_for_telegram([], "2026-06-24")
