"""LinkedIn "Save to PDF" export vs profile.yaml. Fixture text is fictional
and shaped like pdftotext output of a real export."""
import importlib.util
import json
import sys
from pathlib import Path

import yaml

from cv_tailor import linkedin_sync as ls

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_export.txt"

PROFILE = {
    "contact": {"name": "Ana Example", "email": "ana@example.com", "phone": "+00 700 000 111"},
    "experiences": [
        {"id": "acme", "role": "Automation Lead", "company": "Acme Widgets",
         "dates": "Jan 2024 – Present"},
        {"id": "acme_ops", "role": "Operations Analyst", "company": "Acme Widgets",
         "dates": "Mar 2023 – Dec 2023"},
        {"id": "globex", "role": "Data Analyst", "company": "Globex Corporation",
         "dates": "Jun 2021 – Jan 2023"},
        {"id": "hooli", "role": "Consultant", "company": "Hooli", "dates": "Jan 2019 – Jan 2020"},
    ],
}


def test_parse_range():
    assert ls.parse_range("Mar 2026 – Present") == ((2026, 3), None)
    assert ls.parse_range("January 2024 - Present (1 year)") == ((2024, 1), None)
    assert ls.parse_range("Jan 2026 – Apr 2026 (on hold)") == ((2026, 1), (2026, 4))
    assert ls.parse_range("Sept 2020 - Dec 2020") == ((2020, 9), (2020, 12))
    assert ls.parse_range("Shipped things") is None


def test_parse_export_groups_roles_under_companies():
    ex = ls.parse_export(FIXTURE.read_text())
    got = [(p["company"], p["role"], p["start"], p["end"]) for p in ex["positions"]]
    assert got == [
        ("Acme Widgets", "Automation Lead", (2024, 1), None),
        ("Acme Widgets", "Operations Analyst", (2023, 2), (2023, 12)),
        ("Globex", "Junior Analyst", (2021, 6), (2023, 1)),
        ("Initech", "Intern", (2020, 7), (2020, 9)),
    ]
    assert ex["emails"] == ["ana.old@example.org"]


def test_compare_reports_every_kind_of_drift():
    res = ls.compare(PROFILE, ls.parse_export(FIXTURE.read_text()))
    by_field = {i["field"]: i for i in res["issues"]}
    assert not res["ok"]
    assert by_field["email"]["linkedin"] == "ana.old@example.org"
    assert "ana@example.com" in by_field["email"]["fix"]
    assert by_field["phone"]["severity"] == "missing"
    assert "name" not in by_field
    assert by_field["experience:acme_ops.start"]["profile"] == "Mar 2023"
    assert by_field["experience:acme_ops.start"]["linkedin"] == "Feb 2023"
    assert by_field["experience:globex.role"]["linkedin"] == "Junior Analyst"
    assert by_field["experience:hooli"]["severity"] == "missing"
    assert "experience:acme" not in by_field and "experience:acme.start" not in by_field
    extra = [i for i in res["issues"] if i["field"] == "experience:linkedin-only"]
    assert extra and "Initech" in extra[0]["linkedin"]
    assert "Fix:" in ls.to_markdown(res)


def test_clean_match_is_ok():
    prof = {"contact": {"name": "Ana Example", "email": "ana.old@example.org"},
            "experiences": [{"id": "g", "role": "Junior Analyst", "company": "Globex",
                             "dates": "Jun 2021 – Jan 2023"}]}
    text = FIXTURE.read_text()
    res = ls.compare(prof, ls.parse_export(text))
    assert res["ok"]


def test_script_reads_a_txt_export_and_never_writes_the_profile(tmp_path, monkeypatch, capsys):
    prof = tmp_path / "profile.yaml"
    prof.write_text(yaml.safe_dump(PROFILE))
    before = prof.read_text()
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(prof))
    spec = importlib.util.spec_from_file_location("linkedin_sync_cli",
                                                  ROOT / "scripts" / "linkedin_sync.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["linkedin_sync_cli"] = m
    spec.loader.exec_module(m)
    out = tmp_path / "r.json"
    assert m.main([str(FIXTURE), "--json", "--out", str(out)]) == 1
    assert json.loads(out.read_text())["positions_found"] == 4
    assert prof.read_text() == before
    assert m.main([str(tmp_path / "missing.pdf")]) == 2
