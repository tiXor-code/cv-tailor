"""Grounded answerer for job-application screening questions.

This module decides what gets typed into an employer's application form, so
groundedness is the entire point: nothing it returns may be invented, and a
determinate field (a Yes/No radio, a number box) must never be filled with a
value that means something *different* from the grounded fact. Wrong output
here is a lie to an employer.

Pipeline:

1. EEO / demographic tier (no LLM). Protected-characteristic questions are
   policy-driven: pick a "prefer not to answer" option, else skip/None. Label
   matching is word-boundary based so "race conditions" / "harassment training"
   are NOT mistaken for demographics.
2. Deterministic tier (no LLM). Label keywords route to `profile.contact` or
   `answers.yaml`. Routing is word-boundary based (no "capacity"->city,
   "home-based"->location, "portfolio project"->website, or bare-"notice"
   misroutes). Work-authorization is parsed by polarity + jurisdiction;
   salary is currency-aware.
3. LLM tier (gpt-4o-mini, mocked in tests) for anything unmatched. It sees ONLY
   the profile + answers YAML and must reply `UNKNOWN` when ungrounded, or
   `DECLINE` for demographic questions (a backstop if one slips past tier 1).

A FINAL kind/option gate runs on every candidate value from every tier:
select/radio/checkbox values must equal one of the question's options (boolean
facts are mapped to the yes/no option by tiers 2-3 before the gate sees them);
number values must be a bare, unambiguously extractable number; text/textarea
pass through. Anything that fails the gate collapses to UNKNOWN semantics.

`answer_question(...)` returns `None` when a REQUIRED question has no grounded
answer -- the caller (a portal adapter) must abort to `needs_human` rather than
submit a guess. An OPTIONAL question with no grounded answer returns
`Answer("", "policy:skip")` so the caller can leave the field blank.
"""
from __future__ import annotations

import os
import re
from typing import Any, NamedTuple

import yaml


class Question(NamedTuple):
    label: str
    kind: str                  # "text" | "textarea" | "select" | "radio" | "checkbox" | "number"
    required: bool
    options: tuple[str, ...] = ()   # for select/radio/checkbox, else ()


class Answer(NamedTuple):
    value: str                 # text to type or the EXACT option string to pick
    grounded_in: str           # "profile:<path>" | "answers:<key>" | "policy:eeo-decline" |
                                # "policy:skip" | "llm:grounded"


# Sentinel: a category was recognized but cannot be answered safely. Resolves to
# None (required) / policy:skip (optional) -- i.e. "fail closed", NOT fall to LLM.
class _FailClosed:
    __slots__ = ()


_FAIL_CLOSED = _FailClosed()

_OPTION_KINDS = ("select", "radio", "checkbox")


def _fail_closed_result(q: Question) -> Answer | None:
    return None if q.required else Answer("", "policy:skip")


# ---------------------------------------------------------------------------
# EEO / demographic policy tier
# ---------------------------------------------------------------------------

# Word-boundary matches for demographic/EEO-style questions. Checked first and
# handled purely by policy -- never sent to the deterministic/LLM value tiers.
_EEO_RE = re.compile(
    r"\bgender\b|"
    r"\brace\b|\bethnic|"
    r"\bveteran\b|\bdisab|"
    r"\bsexual orientation\b|\bsexual\b|\bsex\b|"
    r"\bpronoun|"
    r"\bhispanic\b|\blatin[oax]\b|\blgbtq\+?|"
    r"\btransgender\b|\bnon-?binary\b|"
    r"\breligio|\bnational origin\b|"
    r"\bdate of birth\b|\bdob\b|\bbirth\s*date\b|\bage\b",
    re.I,
)

# Labels that share a keyword with the demographic list but are plainly NOT
# demographic (a concurrency bug class, compliance training, etc.).
_EEO_EXCLUDE_RE = re.compile(
    r"\brace[\s\-]*conditions?\b|\brace[\s\-]*hazard|\bharassment\b|\btraining\b",
    re.I,
)

# Substrings that mark an option as a "prefer not to answer" style decline.
_DECLINE_PATTERNS = (
    "prefer not",
    "decline to",
    "don't wish",
    "do not wish",
    "not to disclose",
    "not disclose",
    "rather not",
    "choose not to",
)


def _is_eeo_label(label: str) -> bool:
    low = label.lower()
    if _EEO_EXCLUDE_RE.search(low):
        return False
    return bool(_EEO_RE.search(low))


def _find_decline_option(options: tuple[str, ...]) -> str | None:
    for opt in options:
        low = opt.lower()
        if any(p in low for p in _DECLINE_PATTERNS):
            return opt
    return None


def _eeo_answer(q: Question) -> Answer | None:
    """Return the decline answer, a skip, or None (required, no decline option)."""
    decline = _find_decline_option(q.options)
    if decline is not None:
        return Answer(decline, "policy:eeo-decline")
    return _fail_closed_result(q)


# ---------------------------------------------------------------------------
# Value extraction / option-mapping helpers
# ---------------------------------------------------------------------------

_BARE_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")

# yes/no option detection (normalized): match an option that STARTS with a
# yes-word or no-word, so "Yes", "yes", "Y", "No, I don't", etc. are recognized.
_YES_OPTION_RE = re.compile(r"^\s*(?:yes|y|true)\b", re.I)
_NO_OPTION_RE = re.compile(r"^\s*(?:no|n|false)\b", re.I)


def _bare_number(value: str) -> str | None:
    """The value as a single bare number, or None if it isn't unambiguously one.

    "4200" -> "4200". "1200-1500" (a range), "approximately 20 working days"
    (prose) and "30 calendar days" -> None.
    """
    m = _BARE_NUMBER_RE.match(str(value))
    return m.group(1) if m else None


def _match_option(raw: str, options: tuple[str, ...]) -> str | None:
    """Case-insensitive exact match of a reply to one of the options."""
    low = raw.strip().lower()
    for opt in options:
        if opt.strip().lower() == low:
            return opt
    return None


def _find_bool_option(options: tuple[str, ...], want_yes: bool) -> str | None:
    """Return the yes-like or no-like option, or None if there isn't one."""
    pat = _YES_OPTION_RE if want_yes else _NO_OPTION_RE
    for opt in options:
        if pat.match(opt):
            return opt
    return None


def _kind_gate(q: Question, ans: Answer) -> Answer | _FailClosed:
    """Final kind/option validation applied to a candidate answer of any tier.

    Boolean-fact -> option mapping is done by the deterministic/LLM tiers before
    this gate sees the value, so here select/radio/checkbox only needs an exact
    option match. number requires a bare number. text/textarea pass through.
    """
    if q.kind in _OPTION_KINDS:
        matched = _match_option(ans.value, q.options)
        return Answer(matched, ans.grounded_in) if matched is not None else _FAIL_CLOSED
    if q.kind == "number":
        num = _bare_number(ans.value)
        return Answer(num, ans.grounded_in) if num is not None else _FAIL_CLOSED
    return ans


# ---------------------------------------------------------------------------
# Deterministic tier: answers.yaml keyword matching
# ---------------------------------------------------------------------------

def _from_answers(answers: dict, key: str) -> Answer | None:
    value = (answers or {}).get(key)
    if value is None or value == "":
        return None
    return Answer(str(value), f"answers:{key}")


def _start_date_answer(answers: dict) -> Answer | None:
    """Phrase start_availability_days for a free-text box.

    A bare "14" under a label like "Available from" reads as nothing at all,
    so the number is spelled out -- the same reason _salary_answer states its
    currency instead of typing a naked figure.
    """
    raw = _from_answers(answers, "start_availability_days")
    if raw is None:
        return None
    return Answer(f"Within {raw.value} days of an offer", raw.grounded_in)


def _split_contact_name(contact: dict, *, want_last: bool) -> Answer | None:
    """One half of profile.contact.name, for a First/Last question.

    Splits on the first space, mirroring greenhouse's own _split_name. That
    logic is duplicated rather than imported: the portal adapters import this
    module, so importing one of them back into screening would invert the
    layering.

    A single-word name has no surname to give, so a last-name question falls
    through to None rather than repeating the forename -- guessing a surname
    on a real application is worse than leaving it for a human.
    """
    full = (contact or {}).get("name") or ""
    first, _, last = str(full).strip().partition(" ")
    wanted = last.strip() if want_last else first.strip()
    if not wanted:
        return None
    return Answer(wanted, "profile:contact.name")


def _from_contact(contact: dict, key: str) -> Answer | None:
    value = (contact or {}).get(key)
    if value is None or value == "":
        return None
    return Answer(str(value), f"profile:contact.{key}")


# --- routing regexes (word-boundary, per fix 3) ---

_RELOC_RE = re.compile(r"\breloc", re.I)                       # relocate/relocation
_SALARY_RE = re.compile(
    r"\bsalary\b|\bcompensation\b|\bremuneration\b|\bpay expectation|\bexpected pay\b", re.I
)
# A start-date question is NOT a part-time-availability question, but the bare
# \bavailab below claims both. Measured on the live RobCo Ashby form
# 2026-09-16: "Available from" (described there as "Let us know by when you
# would be able to start") was filled with the part-time DAY PATTERN.
# Deliberately narrow: the "availab" arm requires a following "from"/"to
# start", so "What is your availability for part-time work?" still falls
# through to _AVAILAB_RE. The other arms cover phrasings that used to match
# nothing here and fell through to the LLM tier ungrounded.
_START_DATE_RE = re.compile(
    r"\bstart date\b"
    r"|\bwhen (can|could|would) you start\b"
    r"|\bhow soon\b"
    r"|\bearliest\b"
    r"|\bavailab\w* (from|to start)\b",
    re.I,
)
_AVAILAB_RE = re.compile(r"\bavailab", re.I)
_NAME_RE = re.compile(r"\bname\b", re.I)
# Ashby renders First/Last as separate CUSTOM questions, which go through this
# module rather than through greenhouse's own _split_name -- so without these
# two, \bname\b matched both and the live robco form was filled with
# "Teodor-Cristian Lutoiu" in BOTH boxes (measured 2026-09-16). That does not
# park the job, it SUBMITS a wrong value, which is worse.
#
# Deliberately anchored on the name words themselves, never a bare "last" or
# "first": "Company name" and "Project name" must keep falling through to the
# generic rule (and then to None), which the suite already pins.
#
# "Surname" is a separate case worth noting: \bname\b never matched it at all,
# so that phrasing was silently unanswered rather than wrongly answered.
_LAST_NAME_RE = re.compile(r"\blast name\b|\bsurname\b|\bfamily name\b", re.I)
_FIRST_NAME_RE = re.compile(r"\bfirst name\b|\bgiven name\b|\bforename\b", re.I)
_EMAIL_RE = re.compile(r"\bemail\b", re.I)
_PHONE_RE = re.compile(r"\bphone\b|\btelephone\b|\bmobile number\b", re.I)
_WEBSITE_RE = re.compile(
    r"\bwebsite\b|\bportfolio\b(?!\s+(?:project|projects|piece|pieces|item|items|sample|samples))",
    re.I,
)

# notice-period requires "notice" AND an employment-notice context, so that
# "prior notice of criminal proceedings" does NOT route here.
_NOTICE_RE = re.compile(r"\bnotice\b", re.I)
_NOTICE_CTX_RE = re.compile(
    r"\bperiod\b|\bresign\w*|\bleaving\b|\bnotice to leave\b|\bstart\b|\bstart date\b|"
    r"\bcurrent (?:employer|role|job|position)\b|\bhand in\b|\bhow (?:much|long|many)\b",
    re.I,
)


def _is_hourly(label: str) -> bool:
    return bool(re.search(r"\bhourly\b", label) or (re.search(r"\brate\b", label) and re.search(r"\bhour", label)))


def _is_location(label: str) -> bool:
    if re.search(r"\bcity\b", label) or re.search(r"\blocation\b", label):
        return True
    # "Where are you based?" is location; "home-based" / "cloud-based" are not.
    if re.search(r"\bbased\b", label) and not re.search(r"\b[a-z]+-based\b", label):
        return True
    return False


# --- work authorization: polarity + jurisdiction (fix 2) ---

_AUTHORIZED_RE = re.compile(
    r"authoriz|"
    r"\bright to work\b|"
    r"legally\s+(?:able|entitled|allowed|permitted|eligible)\s+to\s+work|"
    r"\beligible to work\b|\bentitled to work\b|\bpermitted to work\b|"
    r"\blegally work\b",
    re.I,
)
_SPONSORSHIP_RE = re.compile(r"sponsor|\bvisa\b", re.I)
_WORK_PERMIT_RE = re.compile(r"\bwork permit\b", re.I)

# Jurisdiction signals. EU/EEA (freedom of movement) vs explicitly non-EU. UK is
# post-Brexit non-EU; Switzerland is non-EU (per spec, authorized NO). "northern
# ireland" is checked before EU "ireland" by scanning the non-EU set first.
_EU_JURISDICTION_RE = re.compile(
    r"\b(?:eu|e\.u\.|eea|e\.e\.a\.|europe|european|european union|european economic area|"
    r"austria|belgium|bulgaria|croatia|cyprus|czech(?:ia)?|denmark|estonia|finland|"
    r"france|germany|greece|hungary|ireland|italy|latvia|lithuania|luxembourg|malta|"
    r"netherlands|poland|portugal|romania|slovakia|slovenia|spain|sweden|"
    r"norway|iceland|liechtenstein)\b",
    re.I,
)
_NON_EU_JURISDICTION_RE = re.compile(
    r"\b(?:usa|u\.s\.a\.|u\.s\.|united states|america|american|"
    r"canada|canadian|uk|u\.k\.|united kingdom|great britain|britain|british|"
    r"england|scotland|wales|northern ireland|"
    r"switzerland|swiss|australia|australian|new zealand|"
    r"china|chinese|india|indian|singapore|japan|japanese|brazil|mexico|"
    r"uae|dubai|qatar|saudi|south africa|nigeria|turkey|russia)\b",
    re.I,
)
# Standalone "US" only counts as the USA when it appears in a jurisdiction-
# shaped context ("authorized to work in the US", "for the US market") --
# never bare, so pronoun uses ("tell us", "let us know") don't misfire.
_US_CONTEXT_RE = re.compile(r"\b(?:in|within|for)\s+(?:the\s+)?us\b", re.I)


def _detect_jurisdiction(label: str) -> str | None:
    low = label.lower()
    # Non-EU first so "northern ireland" isn't swallowed by EU "ireland".
    if _NON_EU_JURISDICTION_RE.search(low):
        return "non_eu"
    # standalone "US" requires a jurisdiction-shaped context ("in/within/for
    # (the) US") so pronoun uses ("tell us", "let us know") are never mistaken
    # for the USA.
    if _US_CONTEXT_RE.search(low):
        return "non_eu"
    if _EU_JURISDICTION_RE.search(low):
        return "eu"
    return None


def _is_work_auth_label(label: str) -> bool:
    return bool(_AUTHORIZED_RE.search(label) or _SPONSORSHIP_RE.search(label) or _WORK_PERMIT_RE.search(label))


def _work_auth_answer(q: Question, answers: dict) -> Answer | _FailClosed | None:
    """Answer a work-authorization question by polarity + jurisdiction.

    For text/textarea, the honest work_authorization prose passes through. For a
    Yes/No-style field we must produce a determinate answer: EU jurisdiction ->
    authorized YES / sponsorship-needed NO; non-EU -> authorized NO /
    sponsorship-needed YES. No jurisdiction (or ambiguous polarity, or no yes/no
    option) -> fall to the LLM grounded in the same text; never the blanket prose.
    """
    raw = _from_answers(answers, "work_authorization")
    if raw is None:
        return None  # no grounded text -> nothing to stand on

    if q.kind not in _OPTION_KINDS:
        return raw  # text/textarea: honest prose pass-through

    label = q.label.lower()
    sponsorship = bool(_SPONSORSHIP_RE.search(label))
    authorized_style = bool(_AUTHORIZED_RE.search(label) or _WORK_PERMIT_RE.search(label))
    if sponsorship and authorized_style:
        return None  # compound/ambiguous polarity -> LLM

    jur = _detect_jurisdiction(label)
    if jur is None:
        return None  # no jurisdiction -> LLM, never the blanket prose

    if authorized_style:
        want_yes = jur == "eu"          # authorized: EU -> Yes, non-EU -> No
    else:                                # sponsorship-style
        want_yes = jur != "eu"          # sponsorship-needed: EU -> No, non-EU -> Yes

    opt = _find_bool_option(q.options, want_yes)
    if opt is None:
        return None  # options aren't yes/no-mappable -> LLM (grounded)
    return Answer(opt, "answers:work_authorization")


# --- relocation yes/no mapping (fix 5) ---

_RELOC_NEG_RE = re.compile(
    r"\bnot\s+(?:open|willing|able|prepared|available|keen|interested)\b|"
    r"\bunwilling\b|\bcannot\b|\bcan'?t\b|\bwon'?t\b|\bno\s+relocation\b|^\s*no\b",
    re.I,
)
_RELOC_POS_RE = re.compile(
    r"\bopen to\b|\bwilling\b|\bhappy to\b|\bkeen to\b|\bprepared to\b|"
    r"\bconsider|\bdiscuss|^\s*yes\b",
    re.I,
)


def _relocation_answer(q: Question, answers: dict) -> Answer | _FailClosed | None:
    raw = _from_answers(answers, "relocation")
    if raw is None:
        return None
    if q.kind not in _OPTION_KINDS:
        return raw  # text/textarea: keep the nuanced prose

    text = str(raw.value).lower()
    if _RELOC_NEG_RE.search(text):
        want_yes = False
    elif _RELOC_POS_RE.search(text):
        want_yes = True
    else:
        return None  # ambiguous -> LLM

    opt = _find_bool_option(q.options, want_yes)
    if opt is None:
        return None
    return Answer(opt, "answers:relocation")


# --- current company/employer (Lever's `org` field: "Current company") ---
#
# There is no profile.contact field for this -- it is grounded in
# profile.experiences instead. Without it, every Lever job dead-ends (the
# field is REQUIRED on every real Lever board), since the deterministic tier
# has nothing else to route it to and the LLM tier (when reachable) would
# have to re-derive the same fact from the full profile dump on every job.

_CURRENT_COMPANY_RE = re.compile(
    r"\bcurrent\s+(?:company|employer|organi[sz]ation)\b|\bpresent\s+employer\b", re.I
)

# Roles that describe self-employment (running one's own business) or
# freelance client work, not being employed BY a company -- excluded when
# picking the "current employer": that field means employer, not "who do
# you invoice". This is why Teodor's Ministeru' Creativ (Founder) and Wolff
# Digital Marketing (freelance) entries are skipped in favor of EA.
_SELF_EMPLOYED_RE = re.compile(r"\bfounder\b|\bfreelance\b", re.I)


def _current_experience(profile: dict) -> dict | None:
    """The most recent profile.experiences entry that reads as genuine
    current employment: `dates` ends in "Present" (profile.yaml's convention
    for an ongoing role) AND neither the role nor the company text reads as
    self-employment or freelance work. Returns None when no experience
    qualifies -- the caller falls through to the LLM tier (if reachable)
    rather than guessing."""
    for exp in (profile or {}).get("experiences", []) or []:
        dates = str(exp.get("dates", "")).strip()
        if not dates.lower().endswith("present"):
            continue
        role = str(exp.get("role", ""))
        company = str(exp.get("company", ""))
        if _SELF_EMPLOYED_RE.search(role) or _SELF_EMPLOYED_RE.search(company):
            continue
        return exp
    return None


def _current_company_answer(q: Question, profile: dict) -> Answer | None:
    """Ground a "current company/employer" question in profile.experiences.
    Returns the employer's `company` field verbatim -- never a shortened or
    invented alias, per the honesty guard. text/select/etc. all go through
    the same final kind/option gate as every other tier, so an option-picklist
    current-company field that doesn't contain this exact string safely
    fails closed rather than guessing."""
    exp = _current_experience(profile)
    if exp is None:
        return None
    company = str(exp.get("company", "")).strip()
    if not company:
        return None
    return Answer(company, "profile:experiences.current")


# --- currency-aware salary (fix 4) ---

_NON_EUR_CURRENCY_RE = re.compile(
    r"\$|£|\busd\b|\bus\$|\bdollars?\b|\bgbp\b|\bpounds?\b|\bchf\b|\bfrancs?\b|"
    r"\bcad\b|\baud\b|\bjpy\b|\byen\b|\bcny\b|\brmb\b|\bsek\b|\bnok\b|\bdkk\b|"
    r"\brubles?\b|\bliras?\b|\bkr\b",
    re.I,
)
_EUR_RE = re.compile(r"€|\beur\b|\beuros?\b", re.I)


def _salary_answer(q: Question, answers: dict) -> Answer | _FailClosed | None:
    label = q.label.lower()
    net = bool(re.search(r"\bnet\b", label))
    key = "salary_fulltime_net_eur_month" if net else "salary_fulltime_gross_eur_month"
    raw = _from_answers(answers, key)
    if raw is None:
        return None  # missing grounded value

    if _NON_EUR_CURRENCY_RE.search(label):
        return _FAIL_CLOSED  # they want a non-EUR figure; we only hold EUR

    eur_explicit = bool(_EUR_RE.search(label))
    band = "net" if net else "gross"

    if q.kind == "number":
        if not eur_explicit:
            return _FAIL_CLOSED  # a bare number without EUR context is ambiguous
        num = _bare_number(raw.value)
        return Answer(num, raw.grounded_in) if num is not None else _FAIL_CLOSED

    if q.kind in _OPTION_KINDS:
        return _FAIL_CLOSED  # salary as a picklist: don't guess

    # text/textarea: state the currency so a US reader can't misread it as USD.
    return Answer(f"{raw.value} EUR {band} per month", raw.grounded_in)


# --- deterministic dispatcher ---

def _deterministic_answer(q: Question, profile: dict, answers: dict) -> Answer | _FailClosed | None:
    label = q.label.lower()
    contact = (profile or {}).get("contact", {}) or {}

    # answers.yaml categories -- checked before generic contact fields so e.g.
    # "relocation" (contains "location") is never mistaken for contact.location.
    if _RELOC_RE.search(label):
        return _relocation_answer(q, answers)
    if _is_hourly(label):
        return _from_answers(answers, "hourly_rate_min_eur")
    if _SALARY_RE.search(label):
        return _salary_answer(q, answers)
    if _NOTICE_RE.search(label) and _NOTICE_CTX_RE.search(label):
        return _from_answers(answers, "notice_period")
    if _is_work_auth_label(label):
        return _work_auth_answer(q, answers)
    if _CURRENT_COMPANY_RE.search(label):
        return _current_company_answer(q, profile)
    # Before the availability rule: a start-date question wants a DATE, and
    # \bavailab would otherwise hand it the part-time day pattern.
    if _START_DATE_RE.search(label):
        return _start_date_answer(answers)
    if _AVAILAB_RE.search(label):
        return _from_answers(answers, "availability_parttime")

    # profile.contact fields
    if _EMAIL_RE.search(label):
        return _from_contact(contact, "email")
    if _PHONE_RE.search(label):
        return _from_contact(contact, "phone")
    if "linkedin" in label:
        return _from_contact(contact, "linkedin")
    if "github" in label:
        return _from_contact(contact, "github")
    if _WEBSITE_RE.search(label):
        return _from_contact(contact, "website")
    if _is_location(label):
        return _from_contact(contact, "location")
    # Before the generic rule: a first/last question wants ONE part, and
    # \bname\b would otherwise hand it the whole string.
    if _LAST_NAME_RE.search(label):
        return _split_contact_name(contact, want_last=True)
    if _FIRST_NAME_RE.search(label):
        return _split_contact_name(contact, want_last=False)
    if _NAME_RE.search(label):
        return _from_contact(contact, "name")

    return None


# ---------------------------------------------------------------------------
# LLM tier
# ---------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = """You answer a single job-application screening question for \
Teodor-Cristian Lutoiu, using ONLY the candidate profile and screening-answers YAML \
provided below. Never invent facts.

RULES:
- Answer using ONLY information present in the profile or answers YAML. Do not guess,
  infer beyond what is stated, or invent numbers, dates, employers, or facts.
- If the question cannot be answered from the given information, reply with exactly:
  UNKNOWN
- If the question asks about demographics or legally protected characteristics
  (gender, race, ethnicity, age, sex, sexual orientation, gender identity, religion,
  disability, veteran status, national origin, marital or family status, pregnancy,
  or similar), reply with exactly: DECLINE
- If the question lists Options, reply with the EXACT text of ONE option (copied
  verbatim), or UNKNOWN if none of them fit.
- Otherwise reply with a short, direct answer (a few words to one sentence). No
  explanations, no markdown, no surrounding quotes, no extra commentary.
- Years-of-experience style questions: answer only if derivable from dates/durations
  actually present in the profile. Do not estimate or round up.
- Work authorization / visa questions: answer strictly from the answers.work_authorization
  text; do not add or infer anything beyond what it says.

Reply with the answer text ONLY (or UNKNOWN, or DECLINE). Nothing else."""


def build_llm_messages(q: Question, profile: dict, answers: dict) -> list[dict]:
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    answers_yaml = yaml.safe_dump(answers, sort_keys=False, allow_unicode=True)
    options_block = ""
    if q.options:
        options_block = "\nOptions (reply with the EXACT text of one, or UNKNOWN):\n" + "\n".join(
            f"- {o}" for o in q.options
        )
    user = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Screening answers (answers.yaml)\n```yaml\n{answers_yaml}```\n\n"
        f"# Question\nLabel: {q.label}\nType: {q.kind}\nRequired: {q.required}{options_block}"
    )
    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _llm_answer(
    q: Question,
    profile: dict,
    answers: dict,
    *,
    client: Any,
    deployment: str | None = None,
) -> Answer | None:
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    response = client.chat.completions.create(
        model=deployment,
        messages=build_llm_messages(q, profile, answers),
        temperature=0.0,
    )
    raw = (response.choices[0].message.content or "").strip()

    # Demographic backstop: the model flags a protected-characteristic question
    # that slipped past the deterministic EEO tier. Checked before option
    # matching (DECLINE is never one of the options).
    if raw.rstrip(".").upper() == "DECLINE":
        decline = _find_decline_option(q.options)
        if decline is not None:
            return Answer(decline, "policy:eeo-decline")
        return _fail_closed_result(q)

    if not raw or raw.strip().upper() == "UNKNOWN":
        return _fail_closed_result(q)

    # Single choke point: the same kind/option gate the deterministic tier
    # uses. select/radio/checkbox must exact-match an option -- a kind="select"
    # with no options() can never be satisfied by raw LLM prose; number must be
    # a bare number; text/textarea pass through.
    gated = _kind_gate(q, Answer(raw, "llm:grounded"))
    if gated is _FAIL_CLOSED:
        return _fail_closed_result(q)
    return gated


# ---------------------------------------------------------------------------
# Open-ended free-text tier
# ---------------------------------------------------------------------------
#
# LLM_SYSTEM_PROMPT above is deliberately a FACTUAL extractor: profile/answers
# only, never invent, a few words to one sentence. A motivation question ("what
# excites you about joining X", "describe the most impactful thing you shipped")
# has no answer in that material, so it correctly replies UNKNOWN -- and both
# live parks on 2026-09-16 were exactly that.
#
# The honest material for those questions already exists and is already being
# SENT with the same application: the tailored cover letter, written from the
# profile + the job description and passed through cover_llm's anti-slop guard.
# Composing an answer from the letter is not a new claim about him; it is the
# claim already going out, restated to fit the box.
#
# Two hard limits keep this from becoming invention:
#   * kind == "textarea" ONLY. That is how both boards render long-form
#     questions, and it keeps salary/years/work-authorization fields on the
#     fail-closed factual path where they belong.
#   * the same anti-slop bar the letter itself must clear. A required question
#     with no clean answer parks; generated filler never goes out under his name.
COMPOSE_SYSTEM_PROMPT = """You answer ONE open-ended job-application question as \
Teodor-Cristian Lutoiu.

You get his cover letter for THIS job (already written and already being sent with
this application) and his profile.

RULES:
- Ground every claim in the letter or the profile. Never add an employer, title,
  date, metric, tool or skill that is not already there.
- A question about motivation, interest or fit ("what excites you about X",
  "why do you want to work here") IS answerable: restate, in his voice, the
  connection the letter already draws between his experience and this role.
  That is a restatement, not a new claim, so do NOT answer it with UNKNOWN.
- Reply with exactly UNKNOWN only when the letter and the profile say nothing
  relevant to what was asked.
- Write 2 to 4 plain sentences. No markdown, no bullet points, no headings, no
  surrounding quotes, no sign-off.
- Plain, direct, specific. No em dashes. Do not write "I am excited", "I am
  eager", "passionate", "dynamic", "leverage", "innovative solutions",
  "proven track record" or similar filler. State the work instead.
- Answer the question that was actually asked. Do not restate the whole letter.

Reply with the answer text ONLY (or UNKNOWN). Nothing else."""

# Same shape as cover_llm.MAX_ATTEMPTS, for the same reason: asked "what excites
# you", the model reaches for precisely the filler the guard bans (measured live
# 2026-09-16: "i am eager", "dynamic", "innovative solutions" in two drafts out
# of two, despite the prompt forbidding them). Feeding the warnings back and
# asking again is what turns that into a clean answer instead of a parked job.
COMPOSE_MAX_ATTEMPTS = 3

_COMPOSE_WORDS_MIN, _COMPOSE_WORDS_MAX = 12, 150


def _compose_warnings(text: str) -> list[str]:
    """The letter's own anti-slop bar, applied to a screening answer.

    Imported inside the function so screening never takes a module-level
    dependency on cover_llm (and cannot create an import cycle with it)."""
    from cv_tailor.cover_llm import BANNED_PHRASES

    warns: list[str] = []
    low = (text or "").lower()
    warns.extend(f"banned phrase: {p!r}" for p in BANNED_PHRASES if p in low)
    if "—" in text or "–" in text:
        warns.append("contains an em/en dash")
    n = len(re.findall(r"\b[\w'-]+\b", text))
    if n < _COMPOSE_WORDS_MIN:
        warns.append(f"too short ({n} words)")
    if n > _COMPOSE_WORDS_MAX:
        warns.append(f"too long ({n} words)")
    return warns


def build_compose_messages(q: Question, profile: dict, context: dict,
                           warnings: list[str] | None = None,
                           previous: str = "") -> list[dict]:
    letter = str((context or {}).get("cover_letter") or "").strip()
    pitch = str((context or {}).get("pitch") or "").strip()
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    pitch_block = f"\n# One-line pitch for this job\n{pitch}\n" if pitch else ""
    # The revision turn: the previous draft AND what was wrong with it, so the
    # model fixes that draft rather than re-rolling into the same filler.
    revision = ""
    if warnings and previous:
        revision = (
            f"\n\n# Your previous draft (rejected)\n{previous}\n\n"
            f"# What was wrong with it\n"
            + "\n".join(f"- {w}" for w in warnings)
            + "\n\nRewrite it so none of those apply. Keep every claim grounded "
              "in the letter and profile."
        )
    user = (
        f"# His cover letter for THIS job\n{letter}\n{pitch_block}\n"
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Question\n{q.label}{revision}"
    )
    return [
        {"role": "system", "content": COMPOSE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _composable(q: Question, context: dict | None) -> bool:
    """Only a long-form question, and only when there is a letter to ground it."""
    return (
        q.kind == "textarea"
        and bool(str((context or {}).get("cover_letter") or "").strip())
    )


def _compose_answer(q: Question, profile: dict, context: dict, *, client: Any,
                    deployment: str | None = None) -> Answer | None:
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    warnings: list[str] = []
    previous = ""
    for _ in range(COMPOSE_MAX_ATTEMPTS):
        response = client.chat.completions.create(
            model=deployment,
            messages=build_compose_messages(q, profile, context, warnings, previous),
            temperature=0.3,
        )
        raw = (response.choices[0].message.content or "").strip()
        # UNKNOWN is the model saying the letter holds nothing relevant. Asking
        # again cannot conjure material, so stop rather than burn attempts.
        if not raw or raw.strip().upper() == "UNKNOWN":
            return _fail_closed_result(q)
        warnings = _compose_warnings(raw)
        if not warnings:
            return Answer(raw, "llm:composed")
        previous = raw
    # Filler that survived every revision is never sent under his name: a
    # required question parks instead.
    return _fail_closed_result(q)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def answer_question(
    q: Question,
    profile: dict,
    answers: dict,
    *,
    client: Any = None,
    deployment: str | None = None,
    context: dict | None = None,
) -> Answer | None:
    """Answer one screening question, deterministic tier first.

    Returns None when the question is REQUIRED and no grounded answer exists
    (caller must abort to needs_human). Returns Answer("", "policy:skip") when
    the question is OPTIONAL and unanswerable.

    client=None means: deterministic tier only. Anything that would need the LLM
    returns None (adapters may run without a client in tests). A category that is
    recognized but cannot be answered safely (wrong currency, unmappable option,
    non-numeric value in a number field) "fails closed": None if required,
    policy:skip if optional -- it does NOT fall through to the LLM.
    """
    if _is_eeo_label(q.label):
        return _eeo_answer(q)

    det = _deterministic_answer(q, profile, answers)
    if det is _FAIL_CLOSED:
        return _fail_closed_result(q)
    if isinstance(det, Answer):
        gated = _kind_gate(q, det)
        if gated is _FAIL_CLOSED:
            return _fail_closed_result(q)
        return gated  # a validated Answer

    # No deterministic answer -> the LLM tier (if a client is available).
    if client is None:
        return None

    # A long-form question with a letter to ground it is composed from that
    # letter, not extracted from the profile: the factual tier can only ever
    # answer UNKNOWN here, and UNKNOWN on a REQUIRED question parks the job.
    # Everything else -- every short or factual field -- stays on the factual
    # path below, even when a letter is available.
    if _composable(q, context):
        return _compose_answer(q, profile, context or {}, client=client,
                               deployment=deployment)

    return _llm_answer(q, profile, answers, client=client, deployment=deployment)
