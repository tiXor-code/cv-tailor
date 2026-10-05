"""cv_tailor.ats_sim -- the ATS simulator. PDFs are rendered at test time from
fictional HTML fixtures (tests/fixtures/ats/) with WeasyPrint and read back
through poppler; DOCX files are assembled in-test with zipfile."""
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from cv_tailor import ats_sim
from cv_tailor.ats_sim import (FAIL, INFO, PASS, WARN, Check, FileReport, cross_file_checks,
                               parse_roles, score_checks, simulate_file, simulate_package)

FIX = Path(__file__).parent / "fixtures" / "ats"
PROFILE = yaml.safe_load((FIX / "profile_ats.yaml").read_text())

needs_poppler = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext missing")


def _status(rep, name):
    return next(c.status for c in rep.checks if c.name == name)


def _pdf(tmp_path, fixture):
    weasyprint = pytest.importorskip("weasyprint")
    out = tmp_path / (Path(fixture).stem + ".pdf")
    weasyprint.HTML(filename=str(FIX / fixture)).write_pdf(str(out))
    return out


# ------------------------------------------------------------------- DOCX kit

BODY = """Jane Example
Springfield - jane.example@example.com - +1 555 010 0199 - linkedin.com/in/jane-example
Summary
Automation engineer who builds retrieval pipelines and internal tools for small teams and documents every system.
Experience
Example Studio — Founder
Mar 2025 – Present
Built a document search service used by twelve client teams across three offices in the region.
Acme Widgets — Content Producer
Jun 2023 – Present
Produced product walkthrough videos for the widget catalogue and partner channels each quarter.
Skills
Python, SQL, Docker, PostgreSQL
Education
BSc Computing — Example University · 2020"""


def _paras(text):
    return "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{ln}</w:t></w:r></w:p>"
                   for ln in text.splitlines())


def _docx(path, body=BODY, *, extra_xml="", header=None, title="Jane Example CV",
          creator="Jane Example", comments=False):
    doc = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body>' + _paras(body) + extra_xml + "</w:body></w:document>")
    core = (f'<cp:coreProperties xmlns:cp="x" xmlns:dc="y"><dc:title>{title}</dc:title>'
            f"<dc:creator>{creator}</dc:creator><cp:lastModifiedBy>{creator}</cp:lastModifiedBy>"
            "</cp:coreProperties>")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc)
        zf.writestr("docProps/core.xml", core)
        if header is not None:
            zf.writestr("word/header1.xml", "<w:hdr>" + _paras(header) + "</w:hdr>")
        if comments:
            zf.writestr("word/comments.xml", "<w:comments/>")
    return path


# --------------------------------------------------------------------- PDF

@needs_poppler
def test_single_column_pdf_scores_clean(tmp_path):
    rep = simulate_file(_pdf(tmp_path, "cv_single.html"), profile=PROFILE)
    assert rep.score == 100, [c for c in rep.checks if c.status in (WARN, FAIL)]
    assert _status(rep, "layout: reading order") == PASS
    assert _status(rep, "text layer") == PASS
    assert rep.contact["email"] == "jane.example@example.com"
    assert rep.contact["linkedin"] == "linkedin.com/in/jane-example"
    assert rep.contact["name"] == "Jane Example"
    assert [(r["company"], r["title"]) for r in rep.roles] == [
        ("Example Studio", "Founder"), ("Acme Widgets", "Content Producer"),
        ("Placeholder Games", "Junior Designer")]


@needs_poppler
def test_concurrent_present_roles_are_info_never_fail(tmp_path):
    rep = simulate_file(_pdf(tmp_path, "cv_single.html"), profile=PROFILE)
    timeline = [c for c in rep.checks if c.name.startswith("timeline")]
    assert any(c.name == "timeline: concurrent roles" and c.status == INFO for c in timeline)
    assert all(c.status != FAIL for c in timeline)


@needs_poppler
def test_two_column_pdf_flags_reading_order_and_junk_title(tmp_path):
    rep = simulate_file(_pdf(tmp_path, "cv_two_column.html"), profile=PROFILE)
    assert _status(rep, "layout: reading order") == FAIL
    assert _status(rep, "metadata: PDF title") == WARN
    assert rep.score < 100


def test_unreadable_pdf_is_a_text_layer_fail_not_a_crash(tmp_path):
    bad = tmp_path / "cv.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf\n")
    rep = simulate_file(bad)
    assert _status(rep, "text layer") == FAIL
    assert rep.score == 85


def test_unsupported_format_fails(tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text("hello")
    assert _status(simulate_file(p), "format") == FAIL


# -------------------------------------------------------------------- DOCX

def test_clean_docx_passes(tmp_path):
    rep = simulate_file(_docx(tmp_path / "cv.docx"), profile=PROFILE)
    bad = [c for c in rep.checks if c.status in (WARN, FAIL)]
    assert bad == []
    assert rep.score == 100
    assert len(rep.roles) == 2


def test_docx_tables_text_boxes_tracked_changes_and_comments(tmp_path):
    extra = ("<w:tbl><w:tr><w:tc>" + _paras("cell") + "</w:tc></w:tr></w:tbl>"
             "<w:p><w:r><w:txbxContent>" + _paras("Hidden box text with keywords") + "</w:txbxContent></w:r></w:p>"
             "<w:p><w:ins w:id=\"1\"><w:r><w:t>inserted</w:t></w:r></w:ins></w:p>")
    rep = simulate_file(_docx(tmp_path / "cv.docx", extra_xml=extra, comments=True), profile=PROFILE)
    assert _status(rep, "layout: tables") == WARN
    assert _status(rep, "layout: text boxes") == FAIL
    assert _status(rep, "metadata: tracked changes") == FAIL
    assert _status(rep, "metadata: comments") == FAIL
    assert "Hidden box text" not in rep.text  # text-box content kept out of the body


def test_docx_contact_only_in_header_fails(tmp_path):
    body = "\n".join(ln for ln in BODY.splitlines() if "@" not in ln)
    rep = simulate_file(_docx(tmp_path / "cv.docx", body=body,
                              header="jane.example@example.com +1 555 010 0199"), profile=PROFILE)
    assert _status(rep, "contact: header/footer") == FAIL
    assert _status(rep, "contact: email") == FAIL


def test_docx_metadata_leftovers(tmp_path):
    rep = simulate_file(_docx(tmp_path / "cv.docx", title="Copy of resume template",
                              creator="Someone Else"), profile=PROFILE)
    assert _status(rep, "metadata: DOCX title") == WARN
    assert _status(rep, "metadata: DOCX author") == WARN


def test_character_hygiene(tmp_path):
    body = BODY.replace("Automation", "Auto­mation").replace("offices", "oﬃces") \
        .replace("Produced", "Pro​duced") + "\nS k i l l e d"
    rep = simulate_file(_docx(tmp_path / "cv.docx", body=body), profile=PROFILE)
    assert _status(rep, "chars: soft hyphens") == WARN
    assert _status(rep, "chars: ligature glyphs") == WARN
    assert _status(rep, "chars: zero-width / invisible chars") == WARN
    assert _status(rep, "chars: spaced-out letters") == FAIL


def test_contact_mismatch_against_profile_fails(tmp_path):
    body = BODY.replace("jane.example@example.com", "old.address@example.org")
    rep = simulate_file(_docx(tmp_path / "cv.docx", body=body), profile=PROFILE)
    assert _status(rep, "contact: email") == FAIL


def test_missing_sections(tmp_path):
    body = BODY.replace("Experience\n", "Journey\n").replace("Skills\n", "Toolbox\n")
    rep = simulate_file(_docx(tmp_path / "cv.docx", body=body), profile=PROFILE)
    assert _status(rep, "section: experience") == FAIL
    assert _status(rep, "section: skills") == WARN


# ------------------------------------------------------------ work history

def test_timeline_gap_warns_and_inverted_dates_fail():
    text = ("Experience\nNorth Co — Analyst\nJan 2024 – Present\n"
            "South Co — Intern\nJan 2020 – Jun 2021\n"
            "West Co — Clerk\nMay 2019 – Jan 2018\nEducation\n")
    rep = FileReport(path="x", kind="pdf", text=text)
    rep.roles = parse_roles(text)
    ats_sim._timeline_checks(rep)
    assert _status(rep, "timeline: gaps") == WARN
    assert _status(rep, "timeline: date order") == FAIL


def test_role_heading_split_keeps_dashes_inside_company():
    roles = parse_roles("Experience\nBig Corp — Games Division — Producer\nAug 2022 – Present\n")
    assert roles[0]["company"] == "Big Corp — Games Division"
    assert roles[0]["title"] == "Producer"
    assert roles[0]["ongoing"] is True


# -------------------------------------------------------------- cross-file

def test_cross_file_flags_email_and_role_date_drift(tmp_path):
    a = _docx(tmp_path / "cv.docx")
    b = _docx(tmp_path / "cv-alt.docx", body=BODY.replace("jane.example@example.com", "jane@example.net")
              .replace("Jun 2023 – Present", "Jul 2023 – Present"))
    result = simulate_package([a, b])
    cross = {c["name"]: c["status"] for c in result["cross_file"]}
    assert cross["cross-file: email"] == FAIL
    assert cross["cross-file: role dates"] == FAIL
    assert cross["cross-file: phone"] == PASS
    assert result["score"] == min(f["score"] for f in result["files"]) - 30


def test_cross_file_needs_two_readable_files():
    assert cross_file_checks([FileReport(path="a", kind="pdf")]) == []


def test_score_formula():
    checks = [Check("a", FAIL), Check("b", WARN), Check("c", WARN), Check("d", INFO), Check("e", PASS)]
    assert score_checks(checks) == 100 - 15 - 10
    assert score_checks([Check(str(i), FAIL) for i in range(10)]) == 0
