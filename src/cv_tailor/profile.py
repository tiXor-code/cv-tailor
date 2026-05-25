"""Profile loader with gap detection.

A "gap" is any string in the profile containing the marker `<fill in`,
which signals that Teodor needs to fill in real content before the
profile can be used for CV generation.
"""
from pathlib import Path
import yaml

GAP_MARKER = "<fill in"


class ProfileGapError(ValueError):
    """Raised when a profile contains unfilled `<fill in ...` markers."""

    def __init__(self, gaps: list[str]):
        self.gaps = gaps
        super().__init__(
            "Profile has unfilled gaps. Fix these before generating a CV:\n  - "
            + "\n  - ".join(gaps)
        )


def load_profile(path: Path | str, strict: bool = False) -> dict:
    """Load a profile YAML file.

    If `strict=True`, raise `ProfileGapError` when any `<fill in ...`
    markers remain in the profile.
    """
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)
    if strict:
        gaps = detect_gaps(profile)
        if gaps:
            raise ProfileGapError(gaps)
    return profile


def detect_gaps(profile, prefix: str = "") -> list[str]:
    """Return dotted-paths to every string in the profile containing `<fill in`."""
    gaps: list[str] = []
    if isinstance(profile, dict):
        for k, v in profile.items():
            path = f"{prefix}.{k}" if prefix else k
            gaps.extend(detect_gaps(v, path))
    elif isinstance(profile, list):
        for i, v in enumerate(profile):
            path = f"{prefix}[{i}]"
            gaps.extend(detect_gaps(v, path))
    elif isinstance(profile, str) and GAP_MARKER in profile:
        gaps.append(prefix)
    return gaps
