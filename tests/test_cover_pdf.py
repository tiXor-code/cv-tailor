"""cover_letter.md -> cover_letter.pdf for forms that want the letter as a file."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("cover_pdf", ROOT / "scripts" / "cover_pdf.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_letter_html_escapes_and_keeps_paragraphs():
    m = _mod()
    out = m.cover_html({"name": "Test User", "email": "test@example.com"}, "Fixture <Co>", "AI Engineer",
                       "First para\nwraps.\n\nSecond para.")
    assert "Fixture &lt;Co&gt;" in out and "<Co>" not in out
    assert out.count('class="para"') == 3          # greeting + two paragraphs
    assert "First para wraps." in out


def test_renders_only_inside_the_queue(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path / "q"))
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(ROOT / "tests" / "fixtures" / "profile_minimal.yaml"))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "cover_letter.md").write_text("x")
    assert m.main([str(outside)]) == 2
    pkg = tmp_path / "q" / "2026-09-28" / "packages" / "p"
    pkg.mkdir(parents=True)
    assert m.main([str(pkg)]) == 3
    (pkg / "cover_letter.md").write_text("I build fixture agents.\n\nSecond.")
    (pkg / "meta.json").write_text(json.dumps({"company": "Fixture Co", "role": "AI Engineer"}))
    assert m.main([str(pkg)]) == 0
    assert (pkg / "cover_letter.pdf").read_bytes().startswith(b"%PDF")
