import json
import sys
import pytest
from unittest.mock import patch, MagicMock

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "tailor.py"


def _load_tailor_module():
    spec = importlib.util.spec_from_file_location("tailor_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tailor_main_writes_artifacts(tmp_path, fixtures_dir, project_root, monkeypatch):
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(fixtures_dir / "profile_minimal.yaml"))
    monkeypatch.setenv("CV_TAILOR_TEMPLATES", str(project_root / "templates"))
    monkeypatch.setenv("CV_TAILOR_JOBS_DIR", str(tmp_path))

    fake_fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fake_fields["job_meta"]["company"] = "Acme"
    fake_fields["job_meta"]["role"] = "Engineer"

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = json.dumps(fake_fields)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response

    mod = _load_tailor_module()
    with patch.object(mod, "build_azure_client", return_value=fake_client):
        result = mod.main([str(fixtures_dir / "sample_jd.txt")])

    job_dir = Path(result["job_dir"])
    assert (job_dir / "jd.txt").exists()
    assert (job_dir / "fields.json").exists()
    assert (job_dir / "cv.html").exists()
    assert (job_dir / "cv.pdf").exists()
    assert (job_dir / "cv.txt").exists()
    assert "acme" in job_dir.name.lower()
