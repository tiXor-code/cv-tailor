"""The queue entry is the only thing the sidecar, /scout and apply_approved
ever read -- apply_options has to survive the write."""
import json
from datetime import date

from cv_tailor.job_sources import JobPosting
from cv_tailor.scout_queue import write_jobs_queue

SCAN_DATE = date(2026, 8, 16)


def _job(**kw):
    base = dict(source="serpapi", org="EnthuZiastic", title="GenAI Engineer",
                location="Anywhere", url="https://www.google.com/search?ibp=htl;jobs",
                description="python automation", raw_id="abc")
    base.update(kw)
    return JobPosting(**base)


def _entry_after_write(tmp_path, job):
    # NOTE the signature: write_jobs_queue(scored, scan_date: date, *, queue_dir)
    # -- scored FIRST, and scan_date is a datetime.date, not a string.
    write_jobs_queue([{"job": job, "score": 8, "reason": "r", "keywords": []}],
                     SCAN_DATE, queue_dir=tmp_path)
    return json.loads((tmp_path / "2026-08-16" / "jobs.json").read_text())[0]


def test_apply_options_reach_the_queue_entry(tmp_path):
    job = _job(apply_options=[
        {"label": "Apply on EnthuZiastic", "url": "https://enthuziastic.com/careers/42"},
    ])
    assert _entry_after_write(tmp_path, job)["apply_options"] == [
        {"label": "Apply on EnthuZiastic", "url": "https://enthuziastic.com/careers/42"},
    ]


def test_source_without_apply_options_writes_empty_list(tmp_path):
    assert _entry_after_write(tmp_path, _job(source="ashby"))["apply_options"] == []
