"""scripts/mark_handoff.py: Teodor ticks a LinkedIn handoff on admin /scout.

Applied -> a ledger row (so no source ever offers that company|role again)
plus the CRM mark; Not applying -> closed with no row. Only from handed_off.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "jobs.db"))
    spec = importlib.util.spec_from_file_location("mark_handoff", ROOT / "scripts" / "mark_handoff.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["mark_handoff"] = m
    spec.loader.exec_module(m)
    crm = []
    monkeypatch.setattr(m, "crm_mark_applied", lambda *a: crm.append(a))
    m._crm_calls = crm
    return m


def _write(tmp_path, status="handed_off"):
    d = tmp_path / "2026-09-24"
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps([{
        "id": "job-1", "status": status, "company": "Examplify", "title": "AI Engineer",
        "url": "https://www.linkedin.com/jobs/view/1", "source": "linkedin"}]))


def _read(tmp_path):
    return json.loads((tmp_path / "2026-09-24" / "jobs.json").read_text())[0]


def test_applied_records_the_ledger_row_and_the_crm(mod, tmp_path):
    _write(tmp_path)
    assert mod.main(["--scan-date", "2026-09-24", "--job-id", "job-1", "--outcome", "applied"]) == 0
    e = _read(tmp_path)
    assert e["status"] == "applied_by_hand" and e.get("applied_at")
    conn = mod.connect(tmp_path / "jobs.db")
    assert mod.application_exists(conn, job_id="other", company="Examplify", role="AI Engineer")
    assert mod._crm_calls == [("Examplify", "AI Engineer", "https://www.linkedin.com/jobs/view/1")]


def test_not_applying_closes_it_with_no_ledger_row(mod, tmp_path):
    _write(tmp_path)
    assert mod.main(["--scan-date", "2026-09-24", "--job-id", "job-1", "--outcome", "not_applying"]) == 0
    assert _read(tmp_path)["status"] == "not_applying"
    conn = mod.connect(tmp_path / "jobs.db")
    assert not mod.application_exists(conn, job_id="job-1", company="Examplify", role="AI Engineer")
    assert mod._crm_calls == []


def test_only_a_handed_off_job_can_be_ticked(mod, tmp_path):
    """A second tap, or a stale page, must not re-record or flip a closed job."""
    _write(tmp_path, status="not_applying")
    assert mod.main(["--scan-date", "2026-09-24", "--job-id", "job-1", "--outcome", "applied"]) == 3
    assert _read(tmp_path)["status"] == "not_applying"


def test_a_crm_failure_does_not_undo_the_tick(mod, tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("sheets down")
    monkeypatch.setattr(mod, "crm_mark_applied", boom)
    _write(tmp_path)
    assert mod.main(["--scan-date", "2026-09-24", "--job-id", "job-1", "--outcome", "applied"]) == 0
    assert _read(tmp_path)["status"] == "applied_by_hand"


def test_unknown_job_is_a_clean_error(mod, tmp_path):
    _write(tmp_path)
    assert mod.main(["--scan-date", "2026-09-24", "--job-id", "nope", "--outcome", "applied"]) == 4
