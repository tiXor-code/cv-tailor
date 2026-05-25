import json
import sys
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "crm_add.py"


def _load():
    spec = importlib.util.spec_from_file_location("crm_add", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_crm_add_appends_row(tmp_path, fixtures_dir):
    job = tmp_path / "2026-05-26-acme-engineer"
    job.mkdir()
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["job_meta"]["company"] = "Acme"
    fields["job_meta"]["role"] = "Engineer"
    (job / "fields.json").write_text(json.dumps(fields))
    (job / "cv.pdf").write_bytes(b"%PDF-1.4\n")

    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = [
        ["Company", "Role", "Location", "JD link", "CV file",
         "Date applied", "Status", "Next action", "Notes"]
    ]

    mod = _load()
    with patch.object(mod, "get_pipeline_worksheet", return_value=fake_ws):
        mod.main([str(job / "fields.json")])

    fake_ws.append_row.assert_called_once()
    row = fake_ws.append_row.call_args.args[0]
    assert row[0] == "Acme"
    assert row[6] == "Saved"
    assert row[4].startswith("file://") and row[4].endswith("cv.pdf")


def test_crm_add_refuses_duplicate_without_force(tmp_path, fixtures_dir):
    job = tmp_path / "j"
    job.mkdir()
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["job_meta"]["company"] = "Acme"
    fields["job_meta"]["role"] = "Engineer"
    (job / "fields.json").write_text(json.dumps(fields))
    (job / "cv.pdf").write_bytes(b"%PDF-1.4\n")

    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = [
        ["Company", "Role", "Location", "JD link", "CV file",
         "Date applied", "Status", "Next action", "Notes"],
        ["acme", "engineer", "", "", "", "", "Saved", "", ""],
    ]

    mod = _load()
    with patch.object(mod, "get_pipeline_worksheet", return_value=fake_ws):
        with pytest.raises(SystemExit) as excinfo:
            mod.main([str(job / "fields.json")])
        assert excinfo.value.code != 0
    fake_ws.append_row.assert_not_called()


def test_crm_add_force_appends_duplicate(tmp_path, fixtures_dir):
    job = tmp_path / "j"
    job.mkdir()
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["job_meta"]["company"] = "Acme"
    fields["job_meta"]["role"] = "Engineer"
    (job / "fields.json").write_text(json.dumps(fields))
    (job / "cv.pdf").write_bytes(b"%PDF-1.4\n")

    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = [
        ["Company", "Role", "Location", "JD link", "CV file",
         "Date applied", "Status", "Next action", "Notes"],
        ["acme", "engineer", "", "", "", "", "Saved", "", ""],
    ]

    mod = _load()
    with patch.object(mod, "get_pipeline_worksheet", return_value=fake_ws):
        mod.main([str(job / "fields.json"), "--force"])

    fake_ws.append_row.assert_called_once()
