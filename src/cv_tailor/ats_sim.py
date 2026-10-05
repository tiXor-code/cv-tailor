"""ATS simulator: parse a generated CV the way a resume parser would and report
what would break.

Reads PDFs through poppler (pdftotext/pdfinfo) and DOCX files through the
word/document.xml inside the zip (stdlib only). Every finding is a Check with
status PASS / WARN / FAIL / INFO; a file's score is 100 - 15 per FAIL - 5 per
WARN (floor 0). INFO never costs points -- it is how legitimate quirks such as
Teodor's concurrent "Present" roles (founder + employee) are reported.

simulate_file() checks one CV; simulate_package() checks every CV file of one
package and adds a cross-file consistency pass (same email, phone, name and
identical dates for the same role across variants).
"""
from __future__ import annotations

import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
FAIL_PENALTY, WARN_PENALTY = 15, 5

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
LINKEDIN_RE = re.compile(r"linkedin\.com/in/[A-Za-z0-9_%-]+", re.IGNORECASE)
SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")

# Heading synonyms an ATS maps to its standard buckets. Matched against a whole
# short line, case-insensitively.
SECTION_ALIASES = {
    "summary": ("summary", "profile", "professional summary", "about", "about me",
                "career summary", "personal statement", "objective"),
    "experience": ("experience", "work experience", "professional experience",
                   "employment", "employment history", "work history", "career history"),
    "skills": ("skills", "technical skills", "core skills", "key skills",
               "competencies", "core competencies", "skills & tools", "skills and tools"),
    "education": ("education", "education & training", "academic background",
                  "qualifications", "education and training"),
}
# Missing Experience breaks work-history parsing outright; the rest degrade it.
SECTION_SEVERITY = {"experience": FAIL, "summary": WARN, "skills": WARN, "education": WARN}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = rf"(?:{_MON}\s+)?(?:19|20)\d{{2}}"
_ONGOING = r"(?:Present|Current|Now|Ongoing|Today)"
DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:[-–—]|to)\s*(?P<end>{_DATE}|{_ONGOING})", re.IGNORECASE)

# Separators between company and title on a role heading line, e.g. our
# template's "Company — Role". The LAST separator splits, because company names
# themselves contain dashes ("Play For Democracy — Arden").
_HEADING_SEP_RE = re.compile(r"\s+[—–|-]\s+|\s+@\s+")
_BULLET_RE = re.compile(r"^\s*[•▪●‣⁃*·-]\s")

JUNK_TITLE_RE = re.compile(r"microsoft word|untitled|template|draft|copy of|\bv\d+\b|\.docx?$",
                           re.IGNORECASE)

# Character classes that read fine on screen but break keyword matching.
_CHAR_CLASSES = [
    ("soft hyphens", re.compile("­"), WARN),
    ("ligature glyphs", re.compile("[ﬀ-ﬆ]"), WARN),
    ("zero-width / invisible chars", re.compile("[​-‍⁠﻿]"), WARN),
    ("unmappable glyphs", re.compile("[�-]"), FAIL),
]

GAP_WARN_MONTHS = 6


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class FileReport:
    path: str
    kind: str
    checks: list[Check] = field(default_factory=list)
    text: str = ""
    contact: dict = field(default_factory=dict)
    roles: list[dict] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def score(self) -> int:
        return score_checks(self.checks)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "kind": self.kind, "score": self.score,
            "contact": self.contact,
            "roles": [{k: v for k, v in r.items() if not k.startswith("_")} for r in self.roles],
            "checks": [c.to_dict() for c in self.checks],
        }


def score_checks(checks: list[Check]) -> int:
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    return max(0, 100 - FAIL_PENALTY * fails - WARN_PENALTY * warns)


# ---------------------------------------------------------------- extraction

def _pdftotext(path: Path, *flags: str) -> str:
    result = subprocess.run(["pdftotext", *flags, "-enc", "UTF-8", str(path), "-"],
                            capture_output=True, text=True, check=True, timeout=60)
    return result.stdout


def _pdfinfo(path: Path) -> dict[str, str]:
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True,
                             check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    info = {}
    for line in out.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            info[key.strip()] = val.strip()
    return info


def _docx_part_text(xml: str) -> str:
    """Visible text of one WordprocessingML part: paragraphs -> lines, tabs and
    breaks kept, deleted runs (w:delText) excluded by construction."""
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"<w:(?:br|cr)\b[^>]*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    runs = re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|(\t)|(\n)", xml)
    text = "".join(a or b or c for a, b, c in runs)
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(ent, ch)
    return text


def _core_prop(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}(?:\s[^>]*)?>([^<]*)</{tag}>", xml)
    return m.group(1).strip() if m else ""


# --------------------------------------------------------------- shared parse

def parse_contact(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    email = EMAIL_RE.search(text)
    phone = next((m.group(0) for m in PHONE_RE.finditer(text)
                  if len(re.sub(r"\D", "", m.group(0))) >= 9
                  and not DATE_RANGE_RE.search(m.group(0))), None)
    linkedin = LINKEDIN_RE.search(text)
    name = None
    for ln in lines[:5]:
        if "@" in ln or re.search(r"\d", ln):
            continue
        words = ln.split()
        if 2 <= len(words) <= 5 and all(w[:1].isupper() for w in words):
            name = ln
            break
    return {
        "name": name,
        "email": email.group(0) if email else None,
        "phone": phone.strip() if phone else None,
        "linkedin": linkedin.group(0) if linkedin else None,
    }


def _heading_key(line: str) -> str | None:
    s = re.sub(r"[:\s]+$", "", line.strip()).lower()
    if not s or len(s) > 40:
        return None
    for key, aliases in SECTION_ALIASES.items():
        if s in aliases:
            return key
    return None


def find_sections(text: str) -> dict[str, int]:
    """section key -> line index of its first heading."""
    found: dict[str, int] = {}
    for i, line in enumerate(text.splitlines()):
        key = _heading_key(line)
        if key and key not in found:
            found[key] = i
    return found


def _ym(token: str, *, end: bool) -> tuple[int, int] | None:
    token = token.strip()
    if re.fullmatch(_ONGOING, token, re.IGNORECASE):
        today = date.today()
        return today.year, today.month
    y = re.search(r"(19|20)\d{2}", token)
    if not y:
        return None
    m = re.match(r"([A-Za-z]{3})", token)
    month = _MONTHS.get(m.group(1).lower()) if m else None
    return int(y.group(0)), month or (12 if end else 1)


def parse_roles(text: str) -> list[dict]:
    """Rebuild work history from the Experience section: every date-range line
    becomes a role whose heading is the same line's leading text or, failing
    that, the nearest preceding non-bullet line."""
    lines = text.splitlines()
    sections = find_sections(text)
    if "experience" not in sections:
        return []
    start = sections["experience"] + 1
    later = [i for i in sections.values() if i > sections["experience"]]
    stop = min(later) if later else len(lines)
    roles = []
    for i in range(start, stop):
        line = lines[i]
        m = DATE_RANGE_RE.search(line)
        if not m:
            continue
        heading = line[:m.start()].strip(" \t·|,")
        if len(heading) < 3:
            heading = ""
            for j in range(i - 1, start - 1, -1):
                prev = lines[j].strip()
                if prev and not _BULLET_RE.match(lines[j]) and not DATE_RANGE_RE.search(prev):
                    heading = prev
                    break
        parts = _HEADING_SEP_RE.split(heading) if heading else []
        company = " — ".join(parts[:-1]).strip() if len(parts) > 1 else heading
        title = parts[-1].strip() if len(parts) > 1 else ""
        ongoing = bool(re.fullmatch(_ONGOING, m.group("end").strip(), re.IGNORECASE))
        roles.append({
            "heading": heading, "company": company, "title": title,
            "dates": re.sub(r"\s+", " ", m.group(0)).strip(),
            "ongoing": ongoing,
            "_start": _ym(m.group("start"), end=False),
            "_end": _ym(m.group("end"), end=True),
        })
    return roles


def _months(a: tuple[int, int], b: tuple[int, int]) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def _timeline_checks(rep: FileReport) -> None:
    roles = [r for r in rep.roles if r["_start"] and r["_end"]]
    inverted = [r["heading"] or r["dates"] for r in roles if _months(r["_start"], r["_end"]) < 0]
    if inverted:
        rep.add("timeline: date order", FAIL, "end before start: " + "; ".join(inverted))
        roles = [r for r in roles if _months(r["_start"], r["_end"]) >= 0]

    ongoing = [r for r in roles if r["ongoing"]]
    if len(ongoing) > 1:
        rep.add("timeline: concurrent roles", INFO,
                f"{len(ongoing)} roles marked Present: "
                + "; ".join(r["company"] or r["heading"] for r in ongoing))
    overlaps = []
    ordered = sorted(roles, key=lambda r: r["_start"])
    for a_i, a in enumerate(ordered):
        for b in ordered[a_i + 1:]:
            if a["ongoing"] and b["ongoing"]:
                continue  # already reported as concurrent
            if _months(b["_start"], a["_end"]) > 0:
                overlaps.append(f"{a['company'] or a['heading']} / {b['company'] or b['heading']}")
    if overlaps:
        # Overlap is a fact of a portfolio career, never a parse failure.
        rep.add("timeline: overlaps", INFO, "; ".join(overlaps))

    gaps = []
    covered_to = None
    for r in ordered:
        if covered_to and _months(covered_to, r["_start"]) > GAP_WARN_MONTHS:
            gaps.append(f"{_months(covered_to, r['_start'])} months before "
                        f"{r['company'] or r['heading']}")
        covered_to = r["_end"] if not covered_to or r["_end"] > covered_to else covered_to
    if gaps:
        rep.add("timeline: gaps", WARN, "; ".join(gaps))
    elif roles:
        rep.add("timeline: gaps", PASS, f"no gap over {GAP_WARN_MONTHS} months")


def _common_checks(rep: FileReport, profile: dict | None) -> None:
    text = rep.text
    lines = [ln for ln in text.splitlines() if ln.strip()]

    # contact parseable (and, when a profile is given, the right contact)
    c = rep.contact = parse_contact(text)
    want = (profile or {}).get("contact", {}) if profile else {}
    if not c["email"]:
        rep.add("contact: email", FAIL, "no email address found in the text layer")
    elif want.get("email") and c["email"].lower() != want["email"].lower():
        rep.add("contact: email", FAIL, f"parsed {c['email']}, profile has {want['email']}")
    else:
        rep.add("contact: email", PASS, c["email"])
    want_phone = re.sub(r"\D", "", str(want.get("phone", "")))
    if not c["phone"]:
        rep.add("contact: phone", FAIL, "no phone number found in the text layer")
    elif want_phone and re.sub(r"\D", "", c["phone"]) != want_phone:
        rep.add("contact: phone", FAIL, f"parsed {c['phone']}, profile has {want.get('phone')}")
    else:
        rep.add("contact: phone", PASS, c["phone"])
    if c["linkedin"]:
        rep.add("contact: linkedin", PASS, c["linkedin"])
    else:
        rep.add("contact: linkedin", WARN, "no linkedin.com/in/ URL found")
    if not c["name"]:
        rep.add("contact: name", WARN, "no name-like line in the first 5 lines")
    elif want.get("name") and c["name"] != want["name"]:
        rep.add("contact: name", WARN, f"parsed {c['name']!r}, profile has {want['name']!r}")
    else:
        rep.add("contact: name", PASS, c["name"])
    head = "\n".join(lines[:8])
    if c["email"] and c["email"] not in head:
        rep.add("contact: position", WARN, "email is not in the first 8 lines; parsers look at the top")

    # standard section headings
    sections = find_sections(text)
    for key, severity in SECTION_SEVERITY.items():
        if key in sections:
            rep.add(f"section: {key}", PASS, f"line {sections[key] + 1}")
        else:
            rep.add(f"section: {key}", severity,
                    f"no standard heading ({', '.join(SECTION_ALIASES[key][:3])}, ...)")

    # work history reconstruction
    rep.roles = parse_roles(text)
    if "experience" in sections:
        if not rep.roles:
            rep.add("work history: roles", FAIL, "no date-ranged roles found under Experience")
        else:
            incomplete = [r["dates"] for r in rep.roles if not (r["company"] and r["title"])]
            if incomplete:
                rep.add("work history: roles", WARN,
                        f"{len(rep.roles)} roles; no company/title split for: " + "; ".join(incomplete))
            else:
                rep.add("work history: roles", PASS, f"{len(rep.roles)} roles with title, company, dates")
            _timeline_checks(rep)

    # character hygiene
    for label, rx, severity in _CHAR_CLASSES:
        hits = rx.findall(text)
        rep.add(f"chars: {label}", severity if hits else PASS,
                f"{len(hits)} found" if hits else "")
    spaced = SPACED_LETTERS_RE.findall(text)
    rep.add("chars: spaced-out letters", FAIL if spaced else PASS,
            ("e.g. " + repr(spaced[0])) if spaced else "")


def _metadata_title_check(rep: FileReport, title: str, label: str) -> None:
    if not title:
        rep.add(f"metadata: {label} title", WARN, "empty; viewers fall back to the filename")
    elif JUNK_TITLE_RE.search(title):
        rep.add(f"metadata: {label} title", WARN, f"leftover title {title!r}")
    else:
        rep.add(f"metadata: {label} title", PASS, title)


# --------------------------------------------------------------- per format

def _backward_jumps(raw: str, layout: str) -> float:
    """How often visual reading order (-layout: line by line, left to right)
    jumps BACK in content-stream order (-raw). Each wide-gap-separated block
    of the layout view is located in the raw text; a single column walks the
    raw text forwards (~0.0), side-by-side columns or tiles bounce between
    them. A parser that reads by visual line sees exactly that scramble."""
    flat = " ".join(raw.split())
    positions = []
    for line in layout.splitlines():
        for block in re.split(r"\s{3,}", line.strip()):
            block = " ".join(block.split())
            if len(block) < 12:
                continue  # short blocks (dates, bullets) are ambiguous
            pos = flat.find(block)
            if pos >= 0:
                positions.append(pos)
    if len(positions) < 2:
        return 0.0
    back = sum(b < a for a, b in zip(positions, positions[1:]))
    return back / (len(positions) - 1)


def _side_by_side_lines(layout: str) -> int:
    """Layout lines carrying two substantial text blocks separated by a wide
    gap -- the signature of columns. A right-aligned date ("Acme   2020 - 2022")
    has one short block, so it doesn't count."""
    n = 0
    for line in layout.splitlines():
        blocks = [b for b in re.split(r"\s{4,}", line.strip()) if b]
        if len(blocks) >= 2 and sum(len(b.split()) >= 4 for b in blocks) >= 2:
            n += 1
    return n


def _pdf(rep: FileReport, path: Path, profile: dict | None) -> None:
    try:
        layout = _pdftotext(path, "-layout")
        raw = _pdftotext(path, "-raw")
    except FileNotFoundError:
        rep.add("text layer", FAIL, "pdftotext not installed; cannot read the PDF")
        return
    except (subprocess.SubprocessError, OSError) as exc:
        rep.add("text layer", FAIL, f"pdftotext could not read the file ({type(exc).__name__})")
        return
    words = len(raw.split())
    if words < 50:
        rep.add("text layer", FAIL, f"only {words} words extractable; image-only or empty PDF")
        return
    rep.add("text layer", PASS, f"{words} words")
    rep.text = raw

    jumps = _backward_jumps(raw, layout)
    side = _side_by_side_lines(layout)
    raw_secs, layout_secs = find_sections(raw), find_sections(layout)
    raw_order = sorted(raw_secs, key=raw_secs.get)
    layout_order = sorted(layout_secs, key=layout_secs.get)
    if raw_order != layout_order and set(raw_order) == set(layout_order):
        rep.add("layout: reading order", FAIL,
                f"sections read as {raw_order} in stream order but {layout_order} visually")
    elif jumps > 0.1:
        rep.add("layout: reading order", FAIL,
                f"multi-column/tile layout scrambles line-by-line parsing "
                f"({jumps:.0%} backward jumps, {side} side-by-side lines)")
    elif jumps > 0.03 or side >= 3:
        rep.add("layout: reading order", WARN,
                f"possible columns: {jumps:.0%} backward jumps, {side} side-by-side lines")
    else:
        rep.add("layout: reading order", PASS, "single column, stream and visual order agree")

    _common_checks(rep, profile)

    info = _pdfinfo(path)
    _metadata_title_check(rep, info.get("Title", ""), "PDF")
    author = info.get("Author", "")
    want_name = ((profile or {}).get("contact") or {}).get("name")
    if author and want_name and author != want_name:
        rep.add("metadata: PDF author", WARN, f"author is {author!r}, not the candidate")
    else:
        rep.add("metadata: PDF author", PASS if author else INFO, author or "no author set")


def _docx(rep: FileReport, path: Path, profile: dict | None) -> None:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        rep.add("text layer", FAIL, f"not a readable DOCX ({type(exc).__name__})")
        return
    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            rep.add("text layer", FAIL, "word/document.xml missing")
            return
        doc = zf.read("word/document.xml").decode("utf-8", "replace")
        hf = "".join(_docx_part_text(zf.read(n).decode("utf-8", "replace"))
                     for n in sorted(names) if re.match(r"word/(header|footer)\d*\.xml$", n))
        core = zf.read("docProps/core.xml").decode("utf-8", "replace") \
            if "docProps/core.xml" in names else ""
        has_comments = "word/comments.xml" in names

    # Text-box content is pulled out separately: many parsers drop it.
    boxes = re.findall(r"<w:txbxContent\b.*?</w:txbxContent>", doc, re.DOTALL)
    body_xml = re.sub(r"<w:txbxContent\b.*?</w:txbxContent>", "", doc, flags=re.DOTALL)
    body = _docx_part_text(body_xml)
    box_text = "".join(_docx_part_text(b) for b in boxes).strip()

    words = len(body.split())
    if words < 50:
        rep.add("text layer", FAIL, f"only {words} words in the document body")
    else:
        rep.add("text layer", PASS, f"{words} words")
    rep.text = body

    tables = len(re.findall(r"<w:tbl>|<w:tbl\s", doc))
    rep.add("layout: tables", WARN if tables else PASS,
            f"{tables} table(s); parsers often flatten cells out of order" if tables else "")
    if box_text:
        rep.add("layout: text boxes", FAIL,
                f"{len(box_text.split())} words inside text boxes, which many ATS skip")
    else:
        rep.add("layout: text boxes", PASS)

    hf_email = EMAIL_RE.search(hf)
    hf_phone = PHONE_RE.search(hf)
    if (hf_email and not EMAIL_RE.search(body)) or (hf_phone and not PHONE_RE.search(body)):
        rep.add("contact: header/footer", FAIL,
                "contact details only in the page header/footer, which ATS often ignore")
    else:
        rep.add("contact: header/footer", PASS)

    _common_checks(rep, profile)

    _metadata_title_check(rep, _core_prop(core, "dc:title"), "DOCX")
    creator = _core_prop(core, "dc:creator")
    modified_by = _core_prop(core, "cp:lastModifiedBy")
    want_name = ((profile or {}).get("contact") or {}).get("name")
    strangers = [n for n in (creator, modified_by) if n and want_name and n != want_name]
    rep.add("metadata: DOCX author", WARN if strangers else PASS,
            f"author/last-modified-by {strangers}" if strangers else (creator or ""))

    tracked = len(re.findall(r"<w:(?:ins|del|moveFrom|moveTo)\b", doc))
    rep.add("metadata: tracked changes", FAIL if tracked else PASS,
            f"{tracked} unaccepted revision(s)" if tracked else "")
    comments = has_comments or "<w:commentReference" in doc
    rep.add("metadata: comments", FAIL if comments else PASS,
            "review comments left in the file" if comments else "")


# ------------------------------------------------------------------ public

def simulate_file(path: Path | str, *, profile: dict | None = None) -> FileReport:
    """Run every check that applies to one CV file (.pdf or .docx)."""
    path = Path(path)
    kind = path.suffix.lower().lstrip(".")
    rep = FileReport(path=str(path), kind=kind)
    if kind == "pdf":
        _pdf(rep, path, profile)
    elif kind == "docx":
        _docx(rep, path, profile)
    else:
        rep.add("format", FAIL, f"unsupported file type .{kind}; send PDF or DOCX")
    return rep


def cross_file_checks(reports: list[FileReport]) -> list[Check]:
    """Every CV variant of one package must agree on contact details and on
    the dates of each role -- recruiters compare them side by side."""
    readable = [r for r in reports if r.text]
    if len(readable) < 2:
        return []
    checks = []
    for key, norm in (("email", str.lower), ("phone", lambda s: re.sub(r"\D", "", s)),
                      ("name", str.strip)):
        values = {norm(r.contact[key]) for r in readable if r.contact.get(key)}
        if len(values) > 1:
            checks.append(Check(f"cross-file: {key}", FAIL, f"differs across files: {sorted(values)}"))
        else:
            checks.append(Check(f"cross-file: {key}", PASS))
    dates: dict[str, set[str]] = {}
    for r in readable:
        for role in r.roles:
            key = (role["company"] or role["heading"]).lower()
            if key:
                dates.setdefault(key, set()).add(role["dates"].replace("—", "–"))
    clashes = [f"{k}: {sorted(v)}" for k, v in dates.items() if len(v) > 1]
    checks.append(Check("cross-file: role dates", FAIL if clashes else PASS, "; ".join(clashes)))
    return checks


def package_result(reports: list[FileReport]) -> dict:
    """Fold per-file reports into one package result. Package score = the
    weakest file's score minus any cross-file penalties (floor 0)."""
    cross = cross_file_checks(reports)
    base = min((r.score for r in reports), default=0)
    penalty = 100 - score_checks(cross)
    return {
        "score": max(0, base - penalty),
        "files": [r.to_dict() for r in reports],
        "cross_file": [c.to_dict() for c in cross],
    }


def simulate_package(paths: list[Path | str], *, profile: dict | None = None) -> dict:
    """Simulate every CV file of one package (see package_result)."""
    return package_result([simulate_file(p, profile=profile) for p in paths])
