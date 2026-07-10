# src/cv_tailor/portal/__init__.py
"""Portal package: autonomous ATS application filling (Ashby/Greenhouse/Lever
adapters land in later tasks). Public surface for the orchestrator and adapters."""
from cv_tailor.portal.base import (
    PortalAdapter,
    PortalResult,
    adapter_for,
    run_portal_application,
)
from cv_tailor.portal import greenhouse  # noqa
from cv_tailor.portal import lever  # noqa

__all__ = ["PortalAdapter", "PortalResult", "adapter_for", "run_portal_application"]
