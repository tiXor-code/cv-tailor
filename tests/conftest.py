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


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.integration unless RUN_INTEGRATION=1."""
    if os.getenv("RUN_INTEGRATION") == "1":
        return
    skip_integration = pytest.mark.skip(reason="set RUN_INTEGRATION=1 to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
