import json
from datetime import date
from types import SimpleNamespace

from cv_tailor.scout_queue import write_jobs_queue, _job_id


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
