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


class ProfileShapeError(ValueError):
    """Raised when a bullet is not a string (e.g. YAML parsed `Foo: bar` as a dict)."""


def load_profile(path: Path | str, strict: bool = False) -> dict:
    """Load a profile YAML file.

    If `strict=True`, raise `ProfileGapError` when any `<fill in ...`
    markers remain in the profile.

    Always validates that every bullet is a string (quoting traps for
    YAML's `key: value` syntax are the most common source of subtle
    rendering bugs).
    """
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)
    _check_bullet_shape(profile)
    if strict:
        gaps = detect_gaps(profile)
        if gaps:
            raise ProfileGapError(gaps)
    return profile


def _check_bullet_shape(profile: dict) -> None:
    """Raise ProfileShapeError if any bullet is not a string."""
    bad: list[str] = []
    for kind in ("experiences", "projects"):
        for i, item in enumerate(profile.get(kind, []) or []):
            for j, bullet in enumerate(item.get("bullets", []) or []):
                if not isinstance(bullet, str):
                    bad.append(f"{kind}[{i}].bullets[{j}] is {type(bullet).__name__}: {bullet!r}")
    if bad:
        raise ProfileShapeError(
            "Bullets must be strings. The most common cause is a colon in an unquoted "
            "YAML string (e.g. `- Own all infra: domain, hosting` parses as a dict). "
            "Quote those bullets. Found:\n  - " + "\n  - ".join(bad)
        )


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
