# src/cv_tailor/portal/__init__.py
"""Portal package: autonomous ATS application filling
(Ashby/Greenhouse/Lever/Micro1 adapters). Public surface for the
orchestrator and adapters."""
from cv_tailor.portal.base import (
    PortalAdapter,
    PortalResult,
    adapter_for,
    run_portal_application,
)
from cv_tailor.portal import ashby  # noqa
from cv_tailor.portal import greenhouse  # noqa
from cv_tailor.portal import lever  # noqa
from cv_tailor.portal import micro1  # noqa

__all__ = ["PortalAdapter", "PortalResult", "adapter_for", "run_portal_application"]
