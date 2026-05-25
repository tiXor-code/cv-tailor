import json
import pytest
from cv_tailor.profile import load_profile
from cv_tailor.render import render_html, render_pdf

def test_render_html_contains_section_headings_in_order(fixtures_dir, project_root):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    html = render_html(profile, fields, template_dir=project_root / "templates")
    expected = ["Summary", "Experience", "Skills", "Education"]
    positions = [html.find(f">{h}<") for h in expected]
    assert all(p > 0 for p in positions), f"missing heading in {positions}"
    assert positions == sorted(positions), "headings out of order"

def test_render_html_includes_contact_line_with_name_and_email(fixtures_dir, project_root):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    html = render_html(profile, fields, template_dir=project_root / "templates")
    assert "Test User" in html
    assert "test@example.com" in html

def test_render_pdf_writes_a_valid_pdf(tmp_path, fixtures_dir, project_root):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    html = render_html(profile, fields, template_dir=project_root / "templates")
    out = tmp_path / "cv.pdf"
    render_pdf(html, css_path=project_root / "templates" / "cv.css", out_path=out)
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")
