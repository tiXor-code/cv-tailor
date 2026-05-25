"""Slug generation for job folders.

Format: <YYYY-MM-DD>-<company-kebab>-<role-kebab>
"""
import re
import unicodedata
from datetime import date


def kebab(s: str) -> str:
    """Lowercase, ASCII-fold, replace non-alnum runs with single hyphens."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def job_slug(company: str, role: str, on: date | None = None) -> str:
    on = on or date.today()
    return f"{on.isoformat()}-{kebab(company)}-{kebab(role)}"
