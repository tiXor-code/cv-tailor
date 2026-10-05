"""Compare a LinkedIn "Save to PDF" export with profile.yaml.

Read-only on both sides: the export is a file Teodor downloaded himself, and
nothing here ever talks to LinkedIn (no scraping, no login: his rule).
profile.yaml is never written either; the output is a list of mismatches with
the fix to make, on whichever side is wrong.

The export is lossy (skills and projects are truncated), so only contact
details, name and the Experience section (company, title, dates to the month)
are compared.
"""
from __future__ import annotations

import re
import subprocess
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_RX = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE_RANGE = re.compile(
    rf"^\s*{_MONTH_RX}\s+(\d{{4}})\s*[-–—]\s*(?:{_MONTH_RX}\s+(\d{{4}})|(Present))\b", re.I)
_DURATION = re.compile(r"^\s*(?:\d+\s+years?)?\s*(?:\d+\s+months?)?\s*$", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_SECTION_END = re.compile(r"^\s*(Education|Licenses|Certifications|Volunteer|Skills|Honors|"
                          r"Languages|Publications|Projects|Recommendations)\s*$", re.I)
_SIDEBAR = {"contact", "top skills", "languages", "certifications", "honors-awards",
            "summary", "experience", "education", "publications", "patents"}


def pdf_to_text(path: Path) -> str:
    """pdftotext (poppler) in reading order. A .txt path is read as-is."""
    path = Path(path)
    if path.suffix.lower() == ".txt":
        return path.read_text(errors="ignore")
    # Absolute path: a file named "-foo.pdf" must never read as a pdftotext option.
    proc = subprocess.run(["pdftotext", str(path.resolve()), "-"], capture_output=True, text=True,
                          timeout=60, check=True)
    return proc.stdout


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _month(name: str, year: str):
    return (int(year), _MONTHS[name[:3].lower()])


def parse_range(text: str):
    """'Mar 2026 – Present' / 'March 2026 - Present (7 months)' ->
    ((2026, 3), None); end is None for Present. None when unparseable."""
    m = _DATE_RANGE.match(text or "")
    if not m:
        return None
    start = _month(m.group(1), m.group(2))
    end = None if m.group(5) else _month(m.group(3), m.group(4))
    return start, end


def _fmt(ym) -> str:
    if ym is None:
        return "Present"
    y, mth = ym
    return f"{[k for k, v in _MONTHS.items() if v == mth][0].title()} {y}"


def parse_export(text: str) -> dict:
    lines = [ln.strip() for ln in (text or "").replace("\f", "\n").splitlines()]
    nonblank = [ln for ln in lines if ln]
    emails = sorted(set(_EMAIL.findall(text or "")))
    phones = sorted({p.strip() for p in _PHONE.findall(text or "")
                     if len(re.sub(r"\D", "", p)) >= 9 and not _DATE_RANGE.match(p)})
    positions = []
    try:
        start = next(i for i, ln in enumerate(nonblank) if ln.lower() == "experience")
    except StopIteration:
        start = None
    if start is not None:
        sect = []
        for ln in nonblank[start + 1:]:
            if _SECTION_END.match(ln):
                break
            sect.append(ln)
        company, prev_date = None, None
        for i, ln in enumerate(sect):
            rng = parse_range(ln)
            if not rng or i == 0:
                continue
            role = sect[i - 1]
            company = _company_for(sect, i, prev_date, company)
            has_loc = i + 1 < len(sect) and not parse_range(sect[i + 1]) and _short(sect[i + 1])
            positions.append({"company": company, "role": role, "start": rng[0], "end": rng[1],
                              "location": sect[i + 1] if has_loc else None})
            prev_date = i
    return {"emails": emails, "phones": phones, "positions": positions, "lines": nonblank}


def _short(line: str) -> bool:
    """Company names and locations are short labels; descriptions are prose."""
    return len(line) <= 60 and not line.rstrip().endswith(".")


def _company_for(sect: list, i: int, prev_date, current):
    """The company of the position whose date range is sect[i] (title at i-1).

    The export groups roles under one company header: `Company`, then a total
    duration line when there are several roles, then title / date range /
    location / description per role. So the line above the title is either a
    duration (company one line further up), the previous role's date or
    location (same company), a description sentence (same company), or a
    short header (a new company)."""
    if i < 2:
        return current
    above = sect[i - 2]
    if _DURATION.match(above) and above.strip():
        return sect[i - 3] if i >= 3 else current
    if prev_date is None:
        return above
    if i - 2 <= prev_date + 1 or parse_range(above):
        return current
    return above if _short(above) else current


def _sim(a: str, b: str) -> float:
    a, b = _fold(a), _fold(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def compare(profile: dict, export: dict) -> dict:
    contact = profile.get("contact") or {}
    issues = []

    def issue(field, profile_value, linkedin_value, fix, severity="mismatch"):
        issues.append({"field": field, "profile": profile_value, "linkedin": linkedin_value,
                       "severity": severity, "fix": fix})

    email = (contact.get("email") or "").strip()
    if email:
        found = [e for e in export["emails"] if e.lower() == email.lower()]
        if not found:
            issue("email", email, ", ".join(export["emails"]) or None,
                  f"Set the LinkedIn contact email to {email}" if export["emails"]
                  else "No email in the export: add the profile email to LinkedIn contact info",
                  "mismatch" if export["emails"] else "missing")
    phone = re.sub(r"\D", "", contact.get("phone") or "")
    if phone:
        digits = [re.sub(r"\D", "", p) for p in export["phones"]]
        if not any(d.endswith(phone[-9:]) for d in digits):
            issue("phone", contact.get("phone"), ", ".join(export["phones"]) or None,
                  f"Set the LinkedIn phone to {contact.get('phone')} (international format)"
                  if digits else "No phone in the export (optional on LinkedIn)",
                  "mismatch" if digits else "missing")
    name = contact.get("name") or ""
    if name and not any(_fold(ln) == _fold(name) for ln in export["lines"]):
        cand = next((ln for ln in export["lines"] if _person_name(ln)), None)
        issue("name", name, cand, f"Use the same name on both: {name}")

    matched = set()
    for exp in profile.get("experiences") or []:
        best, best_score = None, 0.0
        for j, pos in enumerate(export["positions"]):
            if j in matched:
                continue
            score = max(_sim(exp.get("company", ""), pos.get("company") or ""),
                        0.9 * _sim(exp.get("role", ""), pos.get("role") or ""))
            if score > best_score:
                best, best_score = j, score
        label = f"{exp.get('role')} at {exp.get('company')}"
        if best is None or best_score < 0.6:
            issue(f"experience:{exp.get('id')}", label, None,
                  "Not on LinkedIn: add the position, or drop it from the CV if it should not show",
                  "missing")
            continue
        matched.add(best)
        pos = export["positions"][best]
        if _sim(exp.get("role", ""), pos["role"]) < 0.85:
            issue(f"experience:{exp.get('id')}.role", exp.get("role"), pos["role"],
                  f"Pick one title for both (CV says {exp.get('role')!r})")
        if pos.get("company") and _sim(exp.get("company", ""), pos["company"]) < 0.6:
            issue(f"experience:{exp.get('id')}.company", exp.get("company"), pos["company"],
                  "Use the same company name on both")
        rng = parse_range(exp.get("dates") or "")
        if rng is None:
            issue(f"experience:{exp.get('id')}.dates", exp.get("dates"), None,
                  "profile dates are not 'Mon YYYY – Mon YYYY|Present'; cannot compare", "info")
            continue
        for part, idx in (("start", 0), ("end", 1)):
            if rng[idx] != pos[part]:
                issue(f"experience:{exp.get('id')}.{part}", _fmt(rng[idx]), _fmt(pos[part]),
                      f"Make the {part} month agree (CV: {_fmt(rng[idx])}, LinkedIn: {_fmt(pos[part])})")
    for j, pos in enumerate(export["positions"]):
        if j not in matched:
            issue("experience:linkedin-only", None, f"{pos['role']} at {pos['company']}",
                  "On LinkedIn but not in profile.yaml: add it to the profile or confirm it is "
                  "deliberately left off the CV", "extra")
    return {"issues": issues, "positions_found": len(export["positions"]),
            "ok": not any(i["severity"] == "mismatch" for i in issues)}


def _person_name(line: str) -> bool:
    words = line.split()
    return (2 <= len(words) <= 4 and _fold(line) not in _SIDEBAR
            and all(w[:1].isupper() and not re.search(r"\d|@", w) for w in words))


def to_markdown(result: dict) -> str:
    lines = ["# LinkedIn vs profile.yaml", "",
             f"{result['positions_found']} positions read from the export. "
             f"{'No hard mismatches.' if result['ok'] else 'Mismatches found.'}", ""]
    if not result["issues"]:
        lines.append("Everything compared agrees.")
    for i in result["issues"]:
        lines.append(f"- [{i['severity']}] **{i['field']}**: profile `{i['profile']}` vs "
                     f"LinkedIn `{i['linkedin']}`. Fix: {i['fix']}")
    lines += ["", "Skills, projects and recommendations are truncated in the export; "
              "check those side by side by eye."]
    return "\n".join(lines) + "\n"
