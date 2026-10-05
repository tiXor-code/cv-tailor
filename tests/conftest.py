import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES


@pytest.fixture
def project_root():
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def _vetting_never_touches_the_network(monkeypatch):
    """The pay estimate (Azure) and the company check (SerpAPI + Azure) are
    wired into apply_approved, autopilot and adopt_job, so any test driving
    those would otherwise reach the real services whenever the shell or .env
    has keys. The env var also reaches subprocesses; the patched defaults
    fail loudly if anything gets past it. Tests that exercise the checks pass
    their own fake client/search explicitly."""
    monkeypatch.setenv("SCOUT_ENRICH_DISABLED", "1")

    def _blocked(*_a, **_kw):
        raise RuntimeError("network disabled in tests")

    from cv_tailor import company_check, pay_check
    monkeypatch.setattr(pay_check, "_default_client", _blocked)
    monkeypatch.setattr(company_check, "_default_client", _blocked)
    monkeypatch.setattr(company_check, "serp_search", _blocked)


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.integration unless RUN_INTEGRATION=1."""
    if os.getenv("RUN_INTEGRATION") == "1":
        return
    skip_integration = pytest.mark.skip(reason="set RUN_INTEGRATION=1 to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
