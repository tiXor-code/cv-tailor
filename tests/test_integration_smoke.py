import os
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.integration
def test_real_tailor_and_crm_round_trip(tmp_path):
    """Hits real Azure + real Sheets. Set RUN_INTEGRATION=1 to enable."""
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text(
        "Acme Corp is hiring a Python AI Automation Engineer.\n"
        "We want experience with Azure OpenAI, RAG, n8n, and Postgres.\n"
        "Location: Remote (EU).\n"
    )

    os.environ["CV_TAILOR_JOBS_DIR"] = str(tmp_path / "jobs")
    tailor_mod = _load("tailor_script", "scripts/tailor.py")
    result = tailor_mod.main([str(jd_path)])

    job_dir = Path(result["job_dir"])
    assert (job_dir / "cv.pdf").exists()
    assert (job_dir / "fields.json").exists()

    crm_mod = _load("crm_add", "scripts/crm_add.py")
    crm_mod.main([str(job_dir / "fields.json"), "--force"])
