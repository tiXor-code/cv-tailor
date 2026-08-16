"""A manual handoff may have really applied -- its ledger row must survive.
_NO_SUBMIT_REASONS is for reasons that PROVE no submission happened."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load_apply_approved():
    spec = importlib.util.spec_from_file_location(
        "apply_approved_mod", ROOT / "scripts" / "apply_approved.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manual_handoff_reason_is_not_a_proven_non_submission():
    mod = _load_apply_approved()
    assert "handoff-manual: no adapter" not in mod._NO_SUBMIT_REASONS


def test_unattended_no_adapter_is_still_a_proven_non_submission():
    mod = _load_apply_approved()
    assert "no-adapter" in mod._NO_SUBMIT_REASONS
