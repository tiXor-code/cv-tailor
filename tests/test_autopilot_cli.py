"""scripts/autopilot.py CLI: the SCOUT_AUTOPILOT gate and wiring."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "autopilot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("autopilot_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gate_off_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SCOUT_AUTOPILOT", raising=False)
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    mod = _load_module()
    called = []
    mod.run_autopilot = lambda *a, **k: called.append(1)
    assert mod.main([]) == 0
    assert called == []
    assert "disabled" in capsys.readouterr().out.lower()


def test_gate_on_runs_pass_with_telegram_notify(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_AUTOPILOT", "1")
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    day = tmp_path / "2026-07-23"
    day.mkdir()
    (day / "jobs.json").write_text(json.dumps([]))
    mod = _load_module()
    seen = {}

    def fake_run(now=None, *, queue_dir=None, runner=None, notify=None):
        seen["notify"] = notify
        seen["runner"] = runner
        from cv_tailor.autopilot import AutopilotReport
        return AutopilotReport()

    mod.run_autopilot = fake_run
    assert mod.main(["--date", "2026-07-23"]) == 0
    assert seen["notify"] is mod.send_text
    assert seen["runner"] is None  # default production runner


def test_no_telegram_flag_suppresses_notify(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_AUTOPILOT", "1")
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    mod = _load_module()
    seen = {}

    def fake_run(now=None, *, queue_dir=None, runner=None, notify=None):
        seen["notify"] = notify
        from cv_tailor.autopilot import AutopilotReport
        return AutopilotReport()

    mod.run_autopilot = fake_run
    assert mod.main(["--no-telegram"]) == 0
    assert seen["notify"] is None


def test_bad_date_rejected(monkeypatch):
    monkeypatch.setenv("SCOUT_AUTOPILOT", "1")
    mod = _load_module()
    with pytest.raises(SystemExit):
        mod.main(["--date", "../../etc"])
