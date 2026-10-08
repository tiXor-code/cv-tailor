"""docs/cv-rules.md as code.

Two jobs:

- `apply_cv_rules` repairs the only CV text an LLM writes, the headline and
  the summary, before anything renders. A generated value that breaks a rule
  (banned words, pronouns, too long, opens with education, a number that is
  not in profile.yaml) is replaced by the profile's own text, which
  tests/test_profile_rules.py holds to the same rules. Replacing generated
  text with profile text can only remove claims, never add one.
- `lint_cv_text` reports how a rendered CV measures up (banned words, em
  dashes, internal product names, share of bullets with a result). It never
  blocks a build; the report lands in meta.json and on the CLI.
"""
import re

# L2: duty and helper phrases, self-praise, filler adverbs, and words that
# mark text as AI-written. Matched case-insensitively on word boundaries.
BANNED_PHRASES = (
    "responsible for", "duties included", "helped", "assisted", "contributed to",
    "involved in", "various", "results-driven", "proven track record", "passionate",
    "dynamic", "team player", "detail-oriented", "self-motivated", "hard worker",
    "go-getter", "think outside the box", "synergy", "thought leader", "value add",
    "best of breed", "significantly", "successfully", "seamless", "seamlessly",
    "effectively", "efficiently", "leverage", "leveraged", "leveraging", "utilize",
    "utilized", "utilizing", "spearheaded", "robust", "pivotal", "showcasing", "delve",
    "realm", "intricate", "underscore", "cutting-edge", "game-changer", "innovative",
    "vibe coding",
)
_BANNED_RE = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(p) for p in sorted(BANNED_PHRASES, key=len, reverse=True))
    + r")(?![\w-])", re.IGNORECASE)

EM_DASH = "—"

# L5: internal product names. Public products with a live URL may keep theirs.
INTERNAL_NAMES = ("SGEO", "ICP Agent", "AIOS", "SEO Sentinel", "Agent HQ", "Anvil", "Aegis")
_INTERNAL_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in INTERNAL_NAMES) + r")\b")

# P4 / Q5: summary shape.
SUMMARY_MAX_WORDS = 70
SUMMARY_MAX_SENTENCES = 3
HEADLINE_MAX_CHARS = 100
_PRONOUN_RE = re.compile(r"\b(I|me|my|mine|myself|he|him|his|himself)\b")
_EDUCATION_RE = re.compile(r"\b(dissertation|degree|BSc|MSc|university|graduate[ds]?)\b", re.IGNORECASE)

# Standalone numbers only: digits inside names (n8n, C2, M4, Q1) and figures
# with a unit glued on (9.3K, 24GB) are not compared.
_NUMBER_RE = re.compile(r"(?<![A-Za-z\d.,])\d+(?:[.,]\d+)*(?![A-Za-z\d]|[.,]\d)")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_BULLET_LINE_RE = re.compile(r"^\s*[•▪●‣⁃]\s*")


def banned_hits(text: str) -> list[str]:
    """Banned words and phrases found in `text`, lower-cased, in order."""
    return [m.group(1).lower() for m in _BANNED_RE.finditer(text or "")]


def internal_names(text: str) -> list[str]:
    return _INTERNAL_RE.findall(text or "")


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text or "")}


def profile_corpus(profile: dict) -> str:
    """Every piece of profile text a CV may quote numbers from."""
    parts: list[str] = []
    for item in profile.get("summary_pool", []) or []:
        parts.append(str(item.get("text", "")))
    for kind in ("experiences", "projects"):
        for item in profile.get(kind, []) or []:
            for key in ("role", "company", "description", "dates", "name", "tagline"):
                if item.get(key):
                    parts.append(str(item[key]))
            parts.extend(str(b) for b in item.get("bullets", []) or [])
    for ed in profile.get("education", []) or []:
        parts.extend(str(ed.get(k, "")) for k in ("degree", "year", "notes"))
    parts.append(str((profile.get("contact") or {}).get("headline", "")))
    return "\n".join(parts)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def _common_problems(text: str, profile: dict) -> list[str]:
    problems = []
    hits = banned_hits(text)
    if hits:
        problems.append("banned words: " + ", ".join(dict.fromkeys(hits)))
    if EM_DASH in (text or ""):
        problems.append("em dash")
    names = internal_names(text)
    if names:
        problems.append("internal product names: " + ", ".join(dict.fromkeys(names)))
    unknown = sorted(_numbers(text) - _numbers(profile_corpus(profile)))
    if unknown:
        problems.append("numbers not in profile.yaml: " + ", ".join(unknown))
    return problems


def summary_problems(summary: str, profile: dict) -> list[str]:
    """Why a summary breaks docs/cv-rules.md (P4, L1-L3, L5, H1); [] if it passes."""
    text = " ".join((summary or "").split())
    if not text:
        return ["empty"]
    problems = []
    words = len(text.split())
    if words > SUMMARY_MAX_WORDS:
        problems.append(f"{words} words (max {SUMMARY_MAX_WORDS})")
    sentences = _sentences(text)
    if len(sentences) > SUMMARY_MAX_SENTENCES:
        problems.append(f"{len(sentences)} sentences (max {SUMMARY_MAX_SENTENCES})")
    pronouns = _PRONOUN_RE.findall(text)
    if pronouns:
        problems.append("pronouns: " + ", ".join(dict.fromkeys(pronouns)))
    if sentences and _EDUCATION_RE.search(sentences[0]):
        problems.append("opens with education")
    return problems + _common_problems(text, profile)


def headline_problems(headline: str, profile: dict) -> list[str]:
    """Why a headline breaks docs/cv-rules.md (P2, L2, L3); [] if it passes."""
    text = (headline or "").strip()
    if not text:
        return ["empty"]
    problems = []
    if "\n" in text:
        problems.append("more than one line")
    if len(text) > HEADLINE_MAX_CHARS:
        problems.append(f"{len(text)} characters (max {HEADLINE_MAX_CHARS})")
    return problems + _common_problems(text, profile)


def fallback_summary(profile: dict, summary_id: str | None = None) -> str:
    pool = {s["id"]: s for s in profile.get("summary_pool", []) or [] if "id" in s}
    chosen = pool.get(summary_id or "") or next(iter(pool.values()), None)
    return " ".join(str((chosen or {}).get("text", "")).split())


def apply_cv_rules(profile: dict, fields: dict, *, summary_id: str | None = None) -> list[str]:
    """Repair the LLM-written headline and summary in place. Returns notes on
    what was replaced and why (empty when the generated text passed).
    `summary_id` names the profile summary to fall back on (the track's);
    without it the model's chosen_summary_id is used."""
    notes = []
    headline = fields.get("headline")
    problems = headline_problems(headline, profile) if headline else ["missing"]
    if problems:
        fields["headline"] = str((profile.get("contact") or {}).get("headline", "")).strip()
        if headline:
            notes.append("headline replaced with profile headline (" + "; ".join(problems) + ")")
    else:
        fields["headline"] = " ".join(str(headline).split())

    # A project that is already a bullet of a listed role is not repeated
    # (`covered_by` in profile.yaml); dropping an item cannot add a claim.
    # Demos are cut too (R6: "production" and "live" only for systems in use).
    listed = set(fields.get("experience_ids_ordered", []) or [])
    projects = {p["id"]: p for p in profile.get("projects", []) or [] if "id" in p}
    kept = [pid for pid in fields.get("project_ids", []) or []
            if (projects.get(pid) or {}).get("covered_by") not in listed
            and (projects.get(pid) or {}).get("status") != "demo"]
    if len(kept) != len(fields.get("project_ids", []) or []):
        dropped = [pid for pid in fields["project_ids"] if pid not in kept]
        notes.append("projects dropped (demo, or already shown under a role): " + ", ".join(dropped))
        fields["project_ids"] = kept

    summary = fields.get("summary_rewrite", "")
    problems = summary_problems(summary, profile)
    if problems:
        fields["summary_rewrite"] = fallback_summary(
            profile, summary_id or fields.get("chosen_summary_id"))
        notes.append("summary replaced with profile summary (" + "; ".join(problems) + ")")
    return notes


def _bullets_from_text(text: str) -> list[str]:
    """Bullets of an extracted CV text layer, wrapped lines re-joined."""
    bullets: list[str] = []
    current: str | None = None
    for line in (text or "").splitlines():
        if _BULLET_LINE_RE.match(line):
            if current:
                bullets.append(current)
            current = _BULLET_LINE_RE.sub("", line).strip()
        elif current is not None and line.startswith(("  ", "\t")) and line.strip():
            current += " " + line.strip()
        else:
            if current:
                bullets.append(current)
            current = None
    if current:
        bullets.append(current)
    return bullets


def has_result_number(bullet: str) -> bool:
    """A number that is not a year: '51 of 55', '30%', '2,361', '7-person'."""
    return any(not _YEAR_RE.match(n) for n in _numbers(bullet))


def lint_cv_text(text: str) -> dict:
    """Measure an extracted CV against docs/cv-rules.md (Q3, Q4, Q6).
    Never raises; `warnings` is empty when the CV passes."""
    bullets = _bullets_from_text(text)
    with_result = [b for b in bullets if has_result_number(b)]
    ratio = round(len(with_result) / len(bullets), 2) if bullets else 0.0
    banned = list(dict.fromkeys(banned_hits(text)))
    names = list(dict.fromkeys(internal_names(text)))
    dashes = (text or "").count(EM_DASH)
    warnings = []
    if banned:
        warnings.append("banned words: " + ", ".join(banned))
    if dashes:
        warnings.append(f"{dashes} em dash(es)")
    if names:
        warnings.append("internal product names: " + ", ".join(names))
    if bullets and ratio < 0.7:
        warnings.append(f"{len(with_result)} of {len(bullets)} bullets carry a result number "
                        f"({ratio:.0%}; target 70%)")
    return {"bullets": len(bullets), "bullets_with_result": len(with_result),
            "result_ratio": ratio, "banned": banned, "em_dashes": dashes,
            "internal_names": names, "warnings": warnings}
