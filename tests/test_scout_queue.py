import json
from datetime import date
from types import SimpleNamespace

import pytest

from cv_tailor.scout_queue import write_jobs_queue, read_description, update_entry, _job_id


def _job(**kw):
    base = dict(source="ashby", org="Acme", title="AI Engineer",
                location="Remote", url="https://j/1", raw_id="abc", description="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_write_jobs_queue_schema(tmp_path):
    scored = [{"job": _job(), "score": 9, "reason": "good fit", "keywords": ["python", "llm"]}]
    out = write_jobs_queue(scored, date(2026, 6, 24), queue_dir=tmp_path)
    assert out == tmp_path / "2026-06-24" / "jobs.json"
    e = json.loads(out.read_text())[0]
    assert e["company"] == "Acme"
    assert e["title"] == "AI Engineer"
    assert e["score"] == 9
    assert e["why"] == "good fit"
    assert e["matched"] == ["python", "llm"]
    assert e["status"] == "pending"
    assert e["apply_method"] == "portal"
    assert e["apply_target"] == "https://j/1"
    assert e["decided_at"] is None
    assert e["id"] == _job_id(_job())


def test_job_id_stable_and_distinct():
    assert _job_id(_job(raw_id="abc")) == _job_id(_job(raw_id="abc"))
    assert _job_id(_job(raw_id="abc")) != _job_id(_job(raw_id="xyz"))


def test_empty_scored_writes_empty_array(tmp_path):
    out = write_jobs_queue([], date(2026, 6, 24), queue_dir=tmp_path)
    assert json.loads(out.read_text()) == []


def test_descriptions_sidecar_written_and_read(tmp_path):
    jd = "Build LLM agents. Python, async, evals."
    scored = [{"job": _job(description=jd), "score": 9, "reason": "", "keywords": []}]
    out = write_jobs_queue(scored, date(2026, 6, 24), queue_dir=tmp_path)
    # sidecar exists alongside jobs.json, keyed by the same id, JD kept out of jobs.json
    entry = json.loads(out.read_text())[0]
    assert "description" not in entry
    sidecar = tmp_path / "2026-06-24" / "descriptions.json"
    assert json.loads(sidecar.read_text())[entry["id"]] == jd
    assert read_description("2026-06-24", entry["id"], queue_dir=tmp_path) == jd


def test_read_description_missing_is_empty(tmp_path):
    # older scans predate the sidecar -> empty string, no crash
    assert read_description("2020-01-01", "deadbeef", queue_dir=tmp_path) == ""


def test_email_apply_detection_in_queue(tmp_path):
    jd = "To apply, send your CV to jobs@acme.dev with the subject line AI."
    scored = [{"job": _job(description=jd), "score": 9, "reason": "", "keywords": []}]
    out = write_jobs_queue(scored, date(2026, 6, 24), queue_dir=tmp_path)
    entry = json.loads(out.read_text())[0]
    assert entry["apply_method"] == "email"
    assert entry["apply_target"] == "jobs@acme.dev"


def test_update_entry_mutates_and_returns_it(tmp_path):
    scored = [{"job": _job(), "score": 9, "reason": "", "keywords": []}]
    write_jobs_queue(scored, date(2026, 6, 24), queue_dir=tmp_path)
    job_id = _job_id(_job())

    result = update_entry(
        "2026-06-24", job_id, lambda e: e.update(status="approved"), queue_dir=tmp_path
    )

    assert result["status"] == "approved"
    entries = json.loads((tmp_path / "2026-06-24" / "jobs.json").read_text())
    assert entries[0]["status"] == "approved"
    assert entries[0]["id"] == job_id


def test_update_entry_only_touches_the_matching_entry(tmp_path):
    scored = [
        {"job": _job(raw_id="one"), "score": 9, "reason": "", "keywords": []},
        {"job": _job(raw_id="two"), "score": 5, "reason": "", "keywords": []},
    ]
    write_jobs_queue(scored, date(2026, 6, 24), queue_dir=tmp_path)
    target_id = _job_id(_job(raw_id="one"))
    other_id = _job_id(_job(raw_id="two"))

    update_entry("2026-06-24", target_id, lambda e: e.update(status="approved"), queue_dir=tmp_path)

    entries = json.loads((tmp_path / "2026-06-24" / "jobs.json").read_text())
    by_id = {e["id"]: e for e in entries}
    assert by_id[target_id]["status"] == "approved"
    assert by_id[other_id]["status"] == "pending"


def test_update_entry_unknown_id_raises_key_error(tmp_path):
    write_jobs_queue([], date(2026, 6, 24), queue_dir=tmp_path)
    with pytest.raises(KeyError):
        update_entry("2026-06-24", "no-such-id", lambda e: None, queue_dir=tmp_path)


def test_update_entry_write_is_atomic_no_tmp_residue(tmp_path):
    scored = [{"job": _job(), "score": 9, "reason": "", "keywords": []}]
    write_jobs_queue(scored, date(2026, 6, 24), queue_dir=tmp_path)
    job_id = _job_id(_job())

    update_entry("2026-06-24", job_id, lambda e: e.update(status="approved"), queue_dir=tmp_path)

    day_dir = tmp_path / "2026-06-24"
    assert list(day_dir.glob("*.tmp")) == []
    assert (day_dir / "jobs.json").exists()
