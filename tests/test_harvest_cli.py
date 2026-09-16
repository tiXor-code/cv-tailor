# tests/test_harvest_cli.py
"""scripts/harvest.py -- the daily harvest pass run_scan.sh invokes.

Same shape as scripts/autopilot.py: run_scan.sh calls it unconditionally and
an env flag inside the script decides whether anything happens, so turning
the harvester off is one .env edit and never a launchd change.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import harvest as harvest_cli


def test_the_gate_off_means_nothing_is_probed(monkeypatch, capsys):
    """run_scan.sh calls this every morning regardless, so the OFF state has
    to be inert -- and say so, because a silent no-op in a daily log is
    indistinguishable from a harvest that found nothing."""
    monkeypatch.setenv("SCOUT_HARVEST", "0")
    monkeypatch.setattr(harvest_cli, "harvest_and_enrol",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("must not probe while disabled")))

    assert harvest_cli.main([]) == 0
    assert "SCOUT_HARVEST" in capsys.readouterr().out


def test_the_pass_reports_what_it_enrolled(monkeypatch, capsys):
    """The summary line is the only evidence in the daily log, so it must
    name the boards -- the autopilot's 'applied=/parked=' convention."""
    monkeypatch.setenv("SCOUT_HARVEST", "1")
    monkeypatch.setattr(harvest_cli, "seen_companies", lambda: ["Checkly", "Camunda"])
    monkeypatch.setattr(harvest_cli, "harvest_and_enrol",
                        lambda *a, **kw: ["checkly", "camunda"])

    assert harvest_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "harvest:" in out
    assert "checkly" in out and "camunda" in out


def test_a_harvest_failure_never_kills_the_daily_run(monkeypatch, capsys):
    """This runs BEFORE the scan. A dead board API, a locked DB or any other
    fault must not stop the day's scan and autopilot from happening."""
    monkeypatch.setenv("SCOUT_HARVEST", "1")
    monkeypatch.setattr(harvest_cli, "seen_companies", lambda: ["Checkly"])

    def boom(*a, **kw):
        raise RuntimeError("board API down")

    monkeypatch.setattr(harvest_cli, "harvest_and_enrol", boom)

    assert harvest_cli.main([]) == 0
    assert "harvest failed" in capsys.readouterr().out


def test_disable_removes_a_board_without_probing(monkeypatch, capsys):
    """De-enrol has to be a command, not a hand-edit of a generated file."""
    monkeypatch.setenv("SCOUT_HARVEST", "1")
    disabled = []
    monkeypatch.setattr(harvest_cli, "disable_ashby_slug",
                        lambda path, slug: disabled.append(slug))
    monkeypatch.setattr(harvest_cli, "harvest_and_enrol",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("must not probe when disabling")))

    assert harvest_cli.main(["--disable", "badboard"]) == 0
    assert disabled == ["badboard"]
