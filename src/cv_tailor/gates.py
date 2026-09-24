# src/cv_tailor/gates.py
"""Gate 1: free rule-based pre-filter (remote / EU-eligible / target keyword).
Heuristic by design — the LLM scorer is the final arbiter. Goal is to cheaply
drop the obvious-no's before any paid enrichment or scoring call."""
from __future__ import annotations
import re

_REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b|distributed|work from anywhere", re.I)
_GLOBAL_RE = re.compile(r"\bglobal(ly)?\b|\bworldwide\b|\banywhere\b|\bemea\b", re.I)
_EU_RE = re.compile(
    r"\b(eu|europe|european|cet|cest|gmt|uk|united kingdom|ireland|germany|france|spain|"
    r"portugal|netherlands|belgium|poland|romania|bulgaria|italy|austria|switzerland|"
    r"sweden|norway|denmark|finland|estonia|lithuania|latvia|czech|greece|hungary)\b", re.I)
# US-only / non-EU exclusions that override a generic "remote".
_US_ONLY_RE = re.compile(r"us[- ]only|u\.s\.[- ]only|must be (us|united states)[- ]based|"
                         r"us work authorization|gc/?citizen", re.I)
# Hybrid/onsite-cadence phrasing (hybrid, "N days a/per week onsite/in office",
# on-site, in-office). A posting can carry a "Remote" label (e.g. from a broad
# company-wide remote-friendly blurb or a mistagged SerpAPI/Google Jobs card --
# see the EnthuZiastic/Cisco cross-listing incident in MEMORY.md) while still
# anchoring the actual role to an onsite cadence in a specific office.
_HYBRID_ONSITE_RE = re.compile(
    r"\bhybrid\b|\bon-?site\b|\bin[- ]office\b|"
    r"\d+\s*days?\s*(?:per|a)\s*week\s*(?:in|at|on-?site|in[- ]office)",
    re.I)


def _blob(location: str, description: str, cap: int = 2000) -> str:
    return f"{location or ''} {(description or '')[:cap]}"


def is_remote(location: str, description: str) -> bool:
    text = _blob(location, description)
    if _REMOTE_RE.search(text):
        return True
    return False


def is_eu_eligible(location: str, description: str) -> bool:
    text = _blob(location, description)
    if _US_ONLY_RE.search(text):
        return False
    if _GLOBAL_RE.search(text):
        return True
    return bool(_EU_RE.search(text))


def has_target_keyword(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


def _is_hybrid_without_eu_signal(location: str, description: str) -> bool:
    """Cheap Gate 1 drop: a hybrid/onsite-cadence posting with no European
    signal anywhere in location+description is very likely US (or other
    non-EU-region) anchored, regardless of a "Remote" label elsewhere in the
    text -- e.g. a broad "we're a global remote-friendly company" blurb next
    to "Onsite 3 days per week in Raleigh... or San Jose, California".
    Recall-favoring: any EU signal (an EU/EMEA/CET country or region name via
    _EU_RE) keeps the job in even when it is genuinely hybrid-in-Europe (e.g.
    "hybrid, 2 days a week in our Bucharest office")."""
    text = _blob(location, description)
    if not _HYBRID_ONSITE_RE.search(text):
        return False
    return not _EU_RE.search(text)


def passes_gate1(job, keywords: list[str]) -> bool:
    if not is_remote(job.location, job.description):
        return False
    if not is_eu_eligible(job.location, job.description):
        return False
    if _is_hybrid_without_eu_signal(job.location, job.description):
        return False
    return has_target_keyword(f"{job.title} {job.description}", keywords)


def matched_tracks(job, tracks: dict) -> list[str]:
    """Track ids (from `tracks`, e.g. profile.yaml's `tracks:` block) whose
    keyword list matches the job's title+description. Order follows the
    dict's iteration order (config order), so a job matching multiple
    tracks lists them in that order -- the caller decides how to break
    ties."""
    text = f"{job.title} {job.description}"
    return [tid for tid, cfg in tracks.items()
            if has_target_keyword(text, cfg.get("keywords", []))]


# --- Scout's own geo + language gate (Teodor, 2026-09-24) -------------------
# Remote only, from ANY country that will hire him -- US, EU, Asia, Cyprus.
# He is an EU citizen working from Bucharest, so the gate drops only what he
# provably cannot take: an explicit residency / work-authorization / clearance
# requirement outside the EU, or onsite work. Everything else reaches the LLM
# scorer, which reads the whole posting (see match.SCORER_SYSTEM_PROMPT).
# is_eu_eligible above is untouched: norina-jobs still imports it.

_NON_EU_PLACES = (
    r"(?:the\s+)?(?:us|u\.s\.|usa|united\s+states|canada|uk|u\.k\.|united\s+kingdom|"
    r"australia|new\s+zealand|india|brazil|mexico|latam|latin\s+america|israel|"
    r"singapore|japan|philippines|south\s+africa|[a-z]+,\s*(?:ca|ny|tx|wa|ma))"
)
_RESIDENCY_RE = re.compile(
    r"us[- ]only|u\.s\.[- ]only|gc/?citizen|\bw-?2\b(?! or 1099)|"
    r"security\s+clearance|\bclearance\s+(?:is\s+)?required|"
    rf"must\s+(?:be\s+)?(?:located|based|resid(?:e|ing)|live|living)\s+in\s+{_NON_EU_PLACES}\b|"
    rf"(?:authori[sz]ed|eligible|authori[sz]ation)\s+to\s+work\s+in\s+{_NON_EU_PLACES}\b|"
    rf"{_NON_EU_PLACES}\s+work\s+authori[sz]ation|"
    rf"(?:must|need\s+to)\s+(?:reside|live)\s+in\s+{_NON_EU_PLACES}\b|"
    # "work outside Canada for up to 90 days a year" = you live in Canada
    rf"work(?:ing)?\s+(?:from\s+)?outside\s+(?:of\s+)?{_NON_EU_PLACES}\s+for\s+up\s+to",
    re.I)
_ONSITE_LOCATION_RE = re.compile(r"\bhybrid\b|\bon-?site\b|\bin[- ]office\b", re.I)
# Hybrid WORK, however phrased -- LinkedIn labels these "Remote" when the
# search asked for remote (his marks: Redeploy, PwC). Never bare "hybrid":
# "hybrid retrieval"/"hybrid search" are RAG terms.
_HYBRID_WORK_RE = re.compile(
    r"hybrid\s+(?:work(?:ing)?|model|role|position|setup|set-up|schedule|arrangement|"
    r"environment|policy|office|mode)|\bhybride\b|\bhybrid\s*\(|"
    r"(?:work|role|position)\s+is\s+hybrid|this\s+is\s+a\s+hybrid", re.I)
_ONSITE_DAYS_RE = re.compile(
    r"\d+\s*days?\s*(?:per|a)\s*week\s*(?:in|at|on-?site|in[- ]office)", re.I)


def is_scout_geo_eligible(location: str, description: str) -> bool:
    """Remote-anywhere eligibility for Scout. False only on an explicit
    non-EU residency/authorization/clearance requirement, or onsite work: a
    hybrid/onsite location that does not also say remote, or an
    N-days-a-week-in-office cadence with no remote location."""
    loc = location or ""
    if _RESIDENCY_RE.search(_blob(loc, description, cap=6000)):
        return False
    remote_loc = bool(_REMOTE_RE.search(loc))
    if _ONSITE_LOCATION_RE.search(loc) and not remote_loc:
        return False
    if _ONSITE_DAYS_RE.search(_blob("", description)) and not remote_loc:
        return False
    # Hybrid work anywhere in the text is a hard pass (Teodor: "Hybrid, hard
    # pass"), even under a remote location label.
    if _HYBRID_WORK_RE.search(_blob("", description, cap=8000)):
        return False
    return True


# Every language but English and Romanian (the two he works in). Started as
# German/French (his words); widened after his 40 rated postings marked a
# Greek requirement and Dutch-language postings "not English friendly".
_LANG_WORD = (r"(?:german|deutsch|french|fran[cç]ais|dutch|nederlands|greek|swedish|danish|"
              r"norwegian|finnish|polish|czech|slovak|hungarian|italian|spanish|portuguese|"
              r"bulgarian|serbian|croatian|ukrainian|russian|turkish|hebrew|arabic|japanese|"
              r"mandarin|chinese|cantonese|korean|hindi)\b")
_LANG_REQUIRED_RE = re.compile(
    rf"(?:fluen(?:t|cy)|native|business[- ]level|professional|proficien(?:t|cy)|excellent|"
    rf"strong|advanced|(?:c1|c2|b2)(?:\s+level)?)\W+"
    # only connective words between the adjective and the language, so
    # "excellent benefits for our French offices" is not a requirement
    rf"(?:(?:in|of|command|knowledge|skills|written|spoken|verbal|and|level|the|communication|language)\W+){{0,4}}{_LANG_WORD}|"
    rf"{_LANG_WORD}\W+(?:\w+\W+){{0,4}}(?:required|mandatory|a\s+must|must[- ]have|essential|"
    rf"at\s+(?:c1|c2|b2)|(?:c1|c2|b2))|"
    rf"(?:speak|write)\s+{_LANG_WORD}|{_LANG_WORD}[- ]speaking",
    re.I)
_LANG_OPTIONAL_RE = re.compile(
    r"\bplus\b|nice\s+to\s+have|advantage|bonus|preferred|desirable|beneficial|asset|merit", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;\n])\s+")
_WORD_RE = re.compile(r"[a-zà-ÿßăâîșțş]+", re.I)
_EN_STOP = frozenset("the and with you we our for are will your of to in is this".split())
_RO_STOP = frozenset("și si de la în pentru cu care este sau să sa din pe".split())
# Share of common English words below which a posting is not written in
# English. Measured 2026-09-24 on 190 real postings: every non-English one
# (Spanish, Dutch, German, French, Italian) sat under 0.06, every English one
# at 0.08 or above.
_ENGLISH_SHARE_MIN = 0.06


def _not_written_in_english_or_romanian(description: str) -> bool:
    words = [w.lower() for w in _WORD_RE.findall((description or "")[:3000])]
    if len(words) < 20:
        return False
    share = lambda stop: sum(w in stop for w in words) / len(words)  # noqa: E731
    return share(_EN_STOP) < _ENGLISH_SHARE_MIN and share(_RO_STOP) < _ENGLISH_SHARE_MIN


def requires_excluded_language(title: str, description: str) -> bool:
    """True when the job needs a language other than English or Romanian: a
    requirement sentence that is not framed as optional, a "German-speaking"
    title, or a posting not written in English or Romanian. "German is a
    plus" and a mere mention of Germany/France are kept."""
    if re.search(rf"{_LANG_WORD}[- ]speaking|{_LANG_WORD}\s+required", title or "", re.I):
        return True
    if _not_written_in_english_or_romanian(description):
        return True
    for sentence in _SENTENCE_SPLIT_RE.split(description or ""):
        if _LANG_REQUIRED_RE.search(sentence) and not _LANG_OPTIONAL_RE.search(sentence):
            return True
    return False


def passes_gate1_tracks(job, tracks: dict) -> str | None:
    """Scout's track-aware Gate 1: remote, geo-eligible under Scout's
    remote-anywhere rule, no required German/French, and a track keyword.
    Returns the winning track id (first match in config order) or None."""
    if not is_remote(job.location, job.description):
        return None
    if not is_scout_geo_eligible(job.location, job.description):
        return None
    if requires_excluded_language(job.title, job.description):
        return None
    matches = matched_tracks(job, tracks)
    return matches[0] if matches else None
