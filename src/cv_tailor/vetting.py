"""Listing fitness: how much a posting's own text looks like a real, healthy job.

Scores the POSTING, not the company and not the fit. A scam, a ghost job or a
harvesting "talent pool" ad can score 9/10 on fit and still be a waste of an
application under Teodor's name, or worse (fees, ID/bank data up front).

Score 0-100 from a baseline of 75. Red flags deduct, green flags add, each flag
counts once. Bands:
    >= 80   healthy
    55-79   mixed      (look before trusting it)
    < 55    high_risk  (never auto-applied; see autopilot)

Every flag carries the evidence that triggered it, so a human can overrule a
false positive in seconds. Normal tech postings must stay healthy: most patterns
need a contact or money context, and a red-flag phrase preceded by a negation
("we will never ask for a fee") does not count.

The text is untrusted input and is only pattern-matched here; nothing in it is
executed or followed.
"""
from __future__ import annotations

import re

from cv_tailor.pay_check import extract_pay

BASELINE = 75
HEALTHY_MIN = 80
MIXED_MIN = 55
# Above this many EUR gross per month a stated figure is not believable for the
# roles Scout looks at (it is ~420k EUR a year).
IMPLAUSIBLE_MONTHLY_EUR = 35_000
# A stated range whose top is more than this multiple of its bottom says
# nothing about pay.
WIDE_RANGE_RATIO = 2.5

_NEGATION = re.compile(r"\b(?:never|not|no|without|don't|do not|won't|will not|nor)\b[^.!?]{0,160}$", re.I)

_FREE_MAIL = (r"gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|aol|icloud|me|"
              r"proton|protonmail|pm|gmx|mail|yandex|zoho|tutanota|web")

# (id, points, regex). Patterns are compiled case-insensitively.
_RED = [
    ("payment_request", -35,
     r"\b(?:registration|application|training|onboarding|processing|placement|starter|admin(?:istration)?)\s+fee"
     r"|\bpay\s+(?:a|the|an?\s+upfront|for\s+(?:your|the))\s+(?:fee|deposit|training|starter\s+kit|equipment)"
     r"|\b(?:refundable\s+)?deposit\s+(?:is\s+)?required"
     r"|\bpurchase\s+(?:your\s+own\s+)?(?:starter\s+kit|equipment|software\s+license)\s+(?:before|to\s+start)"
     r"|\bsend\s+(?:us\s+)?(?:money|payment|a\s+check|a\s+cheque|gift\s+cards?)"
     r"|\bcash\s+(?:the\s+)?(?:check|cheque)"),
    ("sensitive_data_upfront", -30,
     r"\bsocial\s+security\s+number\b|\bSSN\b|\bbank\s+(?:account\s+)?(?:details|number|statement|login)"
     r"|\b(?:copy|scan|photo)\s+of\s+(?:your\s+)?(?:passport|id\b|id\s+card|driver'?s\s+licen[cs]e)"
     r"|\bpassport\s+(?:number|copy|scan)\b|\bcredit\s+card\s+(?:number|details)\b"
     r"|\brouting\s+number\b|\b(?:national\s+id|CNP)\s+number\b|\bdate\s+of\s+birth\s+and\s+(?:address|id)"),
    ("chat_app_contact", -20,
     r"\b(?:contact|message|text|reach|apply|write|send|dm|ping|interview(?:ed)?|chat)\b(?:\s+\w+){0,4}?\s+"
     r"(?:on|via|through|over|using)\s+(?:whats\s?app|telegram|signal|wechat|viber)\b"
     r"|\b(?:whats\s?app|telegram|signal|viber)\s*(?:me|us|only)?\s*(?:at|on|:)?\s*\+?\d[\d\s().-]{6,}"
     r"|\b(?:whats\s?app|telegram)\s+(?:only|interview)\b"),
    ("free_mail_contact", -15,
     rf"\b[\w.+-]+@(?:{_FREE_MAIL})\.(?:com|net|org|de|fr|ro|co\.uk|ru|me|ch)\b"),
    ("too_good_to_be_true", -20,
     r"\bno\s+(?:prior\s+)?experience\s+(?:is\s+)?(?:necessary|needed|required)\b[^.]{0,80}(?:earn|\$|€|£|per\s+(?:day|week))"
     r"|\b(?:earn|make)\s+(?:up\s+to\s+)?[$€£]\s?\d[\d,.]*k?\s+(?:per|a|an|every)\s+(?:day|week|hour)"
     r"|\bguaranteed\s+(?:income|earnings|pay\s+of)\b|\bget\s+paid\s+(?:daily|instantly|today)\b"
     r"|\bunlimited\s+earning\s+potential\b|\bwork\s+(?:only\s+)?\d\s+hours?\s+a\s+(?:day|week)\s+and\s+earn"),
    ("mlm_commission_only", -25,
     r"\bcommission[- ](?:only|based\s+only)\b|\b100\s?%\s+commission\b|\bmulti[- ]level\s+marketing\b"
     r"|\bMLM\b|\bnetwork\s+marketing\b|\bdownline\b|\bresidual\s+income\b|\bbe\s+your\s+own\s+boss\b"
     r"|\brecruit\s+(?:your\s+)?(?:friends|family|own\s+team)\b"),
    ("urgency_pressure", -10,
     r"\burgent(?:ly)?\s+(?:hiring|needed|required)\b|\bhiring\s+immediately\b"
     r"|\b(?:only|just)\s+\d+\s+(?:spots|positions|places|slots)\s+(?:left|remaining|available)"
     r"|\blimited\s+(?:spots|slots|places)\b|\bact\s+(?:now|fast|quickly)\b|\bapply\s+(?:now|today)\s+before\b"
     r"|\bstart\s+(?:today|tomorrow)\b|\bdon'?t\s+miss\s+(?:out|this)\b"),
    ("evergreen_talent_pool", -10,
     r"\btalent\s+(?:pool|community|network|pipeline)\b|\bfuture\s+(?:opportunities|openings|roles|vacancies)\b"
     r"|\bgeneral\s+application\b|\bopen\s+application\b|\bspeculative\s+application\b|\bevergreen\b"
     r"|\balways\s+(?:looking|hiring|on\s+the\s+lookout)\b|\bno\s+(?:current\s+)?(?:open\s+)?(?:position|vacancy)\b"),
]
_RED_RES = [(fid, pts, re.compile(rx, re.I)) for fid, pts, rx in _RED]

_GREEN = [
    ("structured_sections", 5, None),  # counted, see _structured_sections
    ("interview_process", 5,
     r"\b(?:interview|hiring|recruitment|selection)\s+process\b|\b(?:first|initial|intro(?:ductory)?)\s+(?:call|interview|chat)\b"
     r"|\btechnical\s+(?:interview|assessment|challenge)\b|\btake[- ]home\b|\b(?:final|culture|team)\s+interview\b"
     r"|\bpair[- ]programming\s+session\b"),
    ("concrete_benefits", 5,
     r"\b\d{2}\s+(?:days\s+)?(?:of\s+)?(?:paid\s+)?(?:vacation|holiday|annual\s+leave|pto)\b|\bpaid\s+time\s+off\b"
     r"|\bhealth\s+(?:insurance|care|cover)\b|\bmedical\s+(?:insurance|subscription)\b|\bpension\b|\b401\s?\(?k\)?"
     r"|\b(?:stock\s+options|equity|esop|vsop)\b|\b(?:learning|training|education|conference)\s+budget\b"
     r"|\bparental\s+leave\b|\bmeal\s+(?:vouchers|tickets)\b|\b(?:home\s+office|equipment)\s+(?:budget|stipend|allowance)\b"),
    ("reporting_line", 5,
     r"\breport(?:s|ing)?\s+(?:directly\s+)?(?:in)?to\s+(?:the|our)?\s*(?:CTO|CEO|COO|CPO|founders?|co-?founder"
     r"|head\s+of|VP|vice\s+president|director|(?:engineering|product|team|tech)\s+(?:lead|manager)|lead|manager)\b"
     r"|\byou(?:'ll|\s+will)\s+work\s+(?:directly\s+)?with\s+(?:our|the)\s+(?:CTO|CEO|founders?|head\s+of)\b"),
]
_GREEN_RES = [(fid, pts, re.compile(rx, re.I) if rx else None) for fid, pts, rx in _GREEN]

# Section kinds a structured posting has. Matched anywhere, not only at line
# starts: most sources strip HTML to one whitespace-collapsed line, so there are
# no line starts left to anchor on. A posting needs 3 DISTINCT kinds to count.
_SECTION_KINDS = [
    ("about", r"\babout\s+(?:us|the\s+(?:role|team|company|job|position))\b|\bwho\s+we\s+are\b"),
    ("role", r"\b(?:key\s+)?responsibilities\b|\bwhat\s+you(?:'ll|\s+will)\s+do\b|\b(?:the|your)\s+role\b"
             r"|\bwhat\s+you(?:'ll|\s+will)\s+be\s+doing\b|\byour\s+(?:mission|impact)\b"),
    ("requirements", r"\brequirements\b|\bqualifications\b|\bwhat\s+(?:you(?:'ll|\s+will)\s+(?:bring|need)"
                     r"|we(?:'re|\s+are)\s+looking\s+for)\b|\byour\s+profile\b|\bwho\s+you\s+are\b"
                     r"|\bmust[- ]haves?\b"),
    ("nice_to_have", r"\bnice[- ]to[- ]haves?\b|\bbonus\s+points\b|\bpreferred\s+qualifications\b"),
    ("offer", r"\bwhat\s+we\s+offer\b|\bwhy\s+join\s+us\b|\bbenefits\b|\bperks\b|\bcompensation\b"),
    ("stack", r"\b(?:tech|our)\s+stack\b|\btechnologies\s+we\s+use\b"),
    ("process", r"\b(?:interview|hiring)\s+process\b|\bhow\s+(?:we\s+hire|to\s+apply)\b"),
]
_SECTION_RES = [(k, re.compile(rx, re.I)) for k, rx in _SECTION_KINDS]

_ACRONYMS = {"GDPR", "OAUTH", "GRAPHQL", "NEXTJS", "REACT", "AZURE", "LANGCHAIN", "NODEJS",
             "PYTHON", "KUBERNETES", "DOCKER", "OPENAI", "SAAS", "B2B", "EMEA", "DACH", "USA",
             "CI/CD", "HTML", "JSON", "MLOPS", "LLMS", "NOTE", "REMOTE", "HYBRID", "ONSITE"}


def _snippet(text: str, m: re.Match, pad: int = 0) -> str:
    s = text[max(0, m.start() - pad):m.end() + pad]
    return re.sub(r"\s+", " ", s).strip()[:80]


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATION.search(text[max(0, start - 200):start]))


def _first_unnegated(rx: re.Pattern, text: str) -> re.Match | None:
    for m in rx.finditer(text):
        if not _negated(text, m.start()):
            return m
    return None


def _shouting(text: str) -> str | None:
    """Evidence when the posting shouts: a run of exclamation marks, or many
    all-caps words that are not tech acronyms."""
    bangs = re.findall(r"!{2,}", text)
    words = re.findall(r"\b[A-Z][A-Z!]{4,}\b", text)
    caps = [w for w in words if w.strip("!") not in _ACRONYMS]
    total = max(1, len(re.findall(r"\b\w+\b", text)))
    if len(bangs) >= 2 or text.count("!") >= 8:
        return f"{text.count('!')} exclamation marks"
    if len(caps) >= 6 and len(caps) / total > 0.04:
        return "all-caps: " + " ".join(caps[:4])
    return None


def _structured_sections(text: str) -> list[str]:
    return [kind for kind, rx in _SECTION_RES if rx.search(text)]


def band_for(score: int) -> str:
    if score >= HEALTHY_MIN:
        return "healthy"
    if score >= MIXED_MIN:
        return "mixed"
    return "high_risk"


def vet_listing(text: str) -> dict:
    """{"score": int 0-100, "band": "healthy"|"mixed"|"high_risk",
    "flags": [{"id", "kind": "red"|"green", "points", "evidence"}]}."""
    text = text or ""
    flags: list[dict] = []

    def add(fid: str, kind: str, pts: int, evidence: str) -> None:
        flags.append({"id": fid, "kind": kind, "points": pts, "evidence": evidence})

    for fid, pts, rx in _RED_RES:
        m = _first_unnegated(rx, text)
        if m:
            add(fid, "red", pts, _snippet(text, m))

    stated = extract_pay(text)
    if stated:
        top = max(s["max_eur_month"] for s in stated)
        if top > IMPLAUSIBLE_MONTHLY_EUR and not any(f["id"] == "too_good_to_be_true" for f in flags):
            add("too_good_to_be_true", "red", -20, f"stated pay ~{top} EUR/month")
        wide = [s for s in stated if s["min_eur_month"] > 0
                and s["max_eur_month"] / s["min_eur_month"] > WIDE_RANGE_RATIO]
        if wide:
            add("implausibly_wide_pay_range", "red", -10, wide[0]["text"])

    shout = _shouting(text)
    if shout:
        add("shouting", "red", -10, shout)

    words = len(re.findall(r"\b\w+\b", text))
    if words < 50:
        add("thin_listing", "red", -15, f"{words} words")
    elif words < 100:
        add("thin_listing", "red", -5, f"{words} words")

    if stated:
        add("stated_compensation", "green", 5, stated[0]["text"])
    sections = _structured_sections(text)
    if len(sections) >= 3:
        add("structured_sections", "green", 5, ", ".join(sections[:4]))
    for fid, pts, rx in _GREEN_RES:
        if rx is None:
            continue
        m = rx.search(text)
        if m:
            add(fid, "green", pts, _snippet(text, m))

    score = max(0, min(100, BASELINE + sum(f["points"] for f in flags)))
    return {"score": score, "band": band_for(score), "flags": flags}


def red_flag_summary(fitness: dict, limit: int = 3) -> str:
    """Short human list of the red flags, e.g. 'payment_request, free_mail_contact'."""
    reds = [f["id"].replace("_", " ") for f in (fitness or {}).get("flags", []) if f.get("kind") == "red"]
    return ", ".join(reds[:limit]) or "no single red flag"
