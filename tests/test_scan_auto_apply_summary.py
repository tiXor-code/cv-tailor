"""'2/2 succeeded' on a day where both jobs parked at needs_human is a lie the
log told for weeks. The summary reports what the QUEUE says happened."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import scan  # noqa: E402


def _seed(tmp_path, statuses):
    day = tmp_path / "2026-08-16"
    day.mkdir(parents=True)
    entries = [{"id": f"id{i}", "company": f"Co{i}", "status": "pending"}
               for i in range(len(statuses))]
    (day / "jobs.json").write_text(json.dumps(entries))
    return day, entries


def _runner_setting(day, statuses):
    """Runner that mutates the queue the way apply_approved.py would."""
    def run(scan_date, entry_id, log_path):
        entries = json.loads((day / "jobs.json").read_text())
        idx = [e["id"] for e in entries].index(entry_id)
        entries[idx]["status"] = statuses[idx]
        (day / "jobs.json").write_text(json.dumps(entries))
        return 0
    return run


def test_summary_counts_queue_outcomes_not_exit_codes(tmp_path, capsys):
    day, _ = _seed(tmp_path, ["needs_human", "needs_human"])
    scan.auto_apply_pending("2026-08-16", queue_dir=tmp_path,
                            runner=_runner_setting(day, ["needs_human", "needs_human"]))
    err = capsys.readouterr().err
    assert "needs_human=2" in err
    assert "sent=0" in err
    assert "2/2 succeeded" not in err


def test_summary_reports_a_real_send(tmp_path, capsys):
    day, _ = _seed(tmp_path, ["sent"])
    scan.auto_apply_pending("2026-08-16", queue_dir=tmp_path,
                            runner=_runner_setting(day, ["sent"]))
    err = capsys.readouterr().err
    assert "sent=1" in err


def test_summary_surfaces_a_nonzero_exit_code_separately(tmp_path, capsys):
    day, _ = _seed(tmp_path, ["failed"])

    def run(scan_date, entry_id, log_path):
        entries = json.loads((day / "jobs.json").read_text())
        entries[0]["status"] = "failed"
        (day / "jobs.json").write_text(json.dumps(entries))
        return 1

    scan.auto_apply_pending("2026-08-16", queue_dir=tmp_path, runner=run)
    err = capsys.readouterr().err
    assert "failed=1" in err
    assert "rc!=0: 1" in err


def test_return_value_is_still_id_and_returncode_pairs(tmp_path):
    day, _ = _seed(tmp_path, ["needs_human"])
    out = scan.auto_apply_pending("2026-08-16", queue_dir=tmp_path,
                                  runner=_runner_setting(day, ["needs_human"]))
    assert out == [("id0", 0)]
