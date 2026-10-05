"""Offer valuation against the floors in answers.yaml, plus a counter draft.

Everything here is deterministic arithmetic over what the offer states and
what answers.yaml holds. Nothing is estimated for him: a value the offer does
not give is reported as missing, a floor answers.yaml does not hold is
reported as unknown.

answers.yaml keys read (all optional here):
  salary_fulltime_gross_eur_month   FTE floor, gross EUR per month
  contractor_floor_eur_month        contractor floor, EUR invoiced per month
  salary_target_eur_month           the number he would sign without haggling
  must_haves                        list; see check_must_haves
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

# Rule of thumb, never a floor: a contractor pays their own taxes, benefits,
# leave and bench time, so the same number is a pay cut.
CONTRACTOR_RULE_OF_THUMB = 1.3
EQUIPMENT_AMORTISE_MONTHS = 24
ANCHOR_UPLIFT = 1.15


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,. ]*", str(v))
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    if re.search(r"\d\s*k\b", str(v), re.I):
        val *= 1000
    return val


def floors(answers: dict) -> dict:
    """{fte, contractor, target}: numbers or None when answers.yaml lacks them."""
    answers = answers or {}
    return {
        "fte": _num(answers.get("salary_fulltime_gross_eur_month")),
        "contractor": _num(answers.get("contractor_floor_eur_month")),
        "target": _num(answers.get("salary_target_eur_month")),
    }


def round_up(x: float, step: int = 100) -> int:
    return int(math.ceil(x / step) * step)


def round_down(x: float, step: int = 50) -> int:
    return int(math.floor(x / step) * step)


def rehearsal(answers: dict, *, contractor: bool = False) -> dict:
    """Anchor / fallback / walk-away for the money question.

    walk-away is the floor he set. anchor is his stated target when
    answers.yaml has one, else the floor plus ANCHOR_UPLIFT (marked as derived).
    fallback sits halfway. Every value is None when the floor is unknown."""
    f = floors(answers)
    floor = f["contractor"] if contractor else f["fte"]
    basis = "contractor_floor_eur_month" if contractor else "salary_fulltime_gross_eur_month"
    if floor is None:
        return {"floor_key": basis, "known": False, "anchor": None, "fallback": None,
                "walk_away": None, "anchor_basis": None}
    if f["target"] and f["target"] > floor and not contractor:
        anchor, anchor_basis = round_up(f["target"]), "salary_target_eur_month"
    else:
        anchor, anchor_basis = round_up(floor * ANCHOR_UPLIFT), f"floor + {round((ANCHOR_UPLIFT - 1) * 100)}%"
    fallback = max(round_down((anchor + floor) / 2), int(floor))
    return {"floor_key": basis, "known": True, "anchor": anchor, "fallback": fallback,
            "walk_away": int(floor), "anchor_basis": anchor_basis}


# --- offer parsing -----------------------------------------------------------

_FIELDS = ("base_monthly", "base_annual", "bonus_guaranteed_annual", "bonus_target_annual",
           "bonus_target_pct", "bonus_guaranteed_pct", "benefits_monthly", "equipment_one_off",
           "remote_pct", "pto_days", "notice_days", "employment", "currency", "role", "company",
           "months_per_year")


def parse_offer(raw: str) -> dict:
    """Offer terms from JSON (keys as in _FIELDS) or loose text lines such as
    "Base: 4,500 EUR/month", "Bonus: 10% target", "Remote: 80%",
    "Employment: contractor". Unrecognised text is kept under `notes`."""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("offer JSON must be an object")
        return {k: data[k] for k in data if k in _FIELDS or k == "notes"}
    offer: dict = {}
    notes = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        low = text.lower()
        n = _num(text)
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        matched = True
        if re.search(r"\b(contractor|contract|b2b|freelance|invoice)\b", low) and "employ" not in low.split(":")[0]:
            offer["employment"] = "contractor"
            if n is not None and re.search(r"\b(base|salary|rate|pay|invoice)\b", low):
                _set_base(offer, low, n)
        elif re.search(r"\b(employee|employment|full[- ]?time|permanent|fte)\b", low):
            offer["employment"] = "contractor" if re.search(r"contract|b2b|freelance", low) else "employee"
            if n is not None and re.search(r"\b(base|salary)\b", low):
                _set_base(offer, low, n)
        elif re.search(r"\b(base|salary|gross)\b", low) and n is not None:
            _set_base(offer, low, n)
        elif "bonus" in low:
            kind = "guaranteed" if re.search(r"guarant|fixed|13th", low) else "target"
            if pct:
                offer[f"bonus_{kind}_pct"] = float(pct.group(1))
            elif n is not None:
                offer[f"bonus_{kind}_annual"] = n * (12 if re.search(r"month|/mo", low) else 1)
        elif re.search(r"\b(benefit|meal|insurance|allowance|stipend)\b", low) and n is not None:
            offer["benefits_monthly"] = offer.get("benefits_monthly", 0) + n / (12 if re.search(r"year|annual|/yr", low) else 1)
        elif re.search(r"\b(equipment|laptop|hardware)\b", low) and n is not None:
            offer["equipment_one_off"] = n
        elif "remote" in low or "hybrid" in low or "on-site" in low or "onsite" in low:
            if pct:
                offer["remote_pct"] = float(pct.group(1))
            elif "fully remote" in low or "100" in low or re.search(r"remote:\s*(yes|full)", low):
                offer["remote_pct"] = 100.0
            elif "on-site" in low or "onsite" in low:
                offer["remote_pct"] = 0.0
            elif "hybrid" in low or re.search(r"\bin (the )?office\b", low):
                # "hybrid, 2 days in office" is not full remote: 3 of 5 days
                days = re.search(r"(\d)\s*days?\b[^.]*\boffice\b", low)
                offer["remote_pct"] = round(100 * (5 - int(days.group(1))) / 5, 1) if days and int(days.group(1)) <= 5 else 50.0
            elif re.search(r"full(y)?[- ]remote|remote[- ]first|remote only|remote-only", low):
                offer["remote_pct"] = 100.0
            else:
                matched = False
        elif re.search(r"\b(pto|vacation|holiday|leave|days off)\b", low) and n is not None:
            offer["pto_days"] = n
        elif "notice" in low and n is not None:
            offer["notice_days"] = n * (30 if "month" in low else 7 if "week" in low else 1)
        elif low.startswith(("role", "title", "position")):
            offer["role"] = text.split(":", 1)[-1].strip()
        elif low.startswith("company"):
            offer["company"] = text.split(":", 1)[-1].strip()
        else:
            matched = False
        if re.search(r"\b(usd|\$|gbp|£|ron|lei|chf)\b|[$£]", low):
            cur = re.search(r"\b(usd|gbp|ron|lei|chf)\b|[$£]", low).group(0)
            offer["currency"] = {"$": "USD", "£": "GBP", "lei": "RON"}.get(cur, cur.upper())
        if not matched:
            notes.append(text)
    if notes:
        offer["notes"] = notes
    return offer


def _set_base(offer: dict, low: str, n: float) -> None:
    if re.search(r"year|annual|/yr|p\.a\.|per annum", low):
        offer["base_annual"] = n
    else:
        offer["base_monthly"] = n


# --- valuation ---------------------------------------------------------------

def value_offer(offer: dict) -> dict:
    months = _num(offer.get("months_per_year")) or 12
    base = _num(offer.get("base_monthly"))
    if base is None and _num(offer.get("base_annual")) is not None:
        base = _num(offer["base_annual"]) / months
    annual_base = (base or 0) * months
    g = _num(offer.get("bonus_guaranteed_annual"))
    if g is None and _num(offer.get("bonus_guaranteed_pct")) is not None:
        g = annual_base * _num(offer["bonus_guaranteed_pct"]) / 100
    t = _num(offer.get("bonus_target_annual"))
    if t is None and _num(offer.get("bonus_target_pct")) is not None:
        t = annual_base * _num(offer["bonus_target_pct"]) / 100
    benefits = _num(offer.get("benefits_monthly")) or 0.0
    equipment = (_num(offer.get("equipment_one_off")) or 0.0) / EQUIPMENT_AMORTISE_MONTHS
    # 13+ salaries a year are guaranteed pay spread over 12 months.
    guaranteed_monthly = (base or 0) * months / 12 + (g or 0) / 12 + benefits + equipment
    missing = [k for k, v in (("base", base), ("remote_pct", offer.get("remote_pct")),
                              ("pto_days", offer.get("pto_days")),
                              ("notice_days", offer.get("notice_days")),
                              ("employment", offer.get("employment"))) if v is None]
    return {
        "employment": offer.get("employment") or "unknown",
        "currency": (offer.get("currency") or "EUR").upper(),
        "base_monthly": round(base, 2) if base is not None else None,
        "bonus_guaranteed_monthly": round((g or 0) / 12, 2),
        "bonus_target_monthly": round((t or 0) / 12, 2),
        "benefits_monthly": round(benefits, 2),
        "equipment_monthly": round(equipment, 2),
        "total_guaranteed_monthly": round(guaranteed_monthly, 2) if base is not None else None,
        "total_with_target_monthly": round(guaranteed_monthly + (t or 0) / 12, 2) if base is not None else None,
        "remote_pct": _num(offer.get("remote_pct")),
        "pto_days": _num(offer.get("pto_days")),
        "notice_days": _num(offer.get("notice_days")),
        "missing": missing,
    }


def compare_to_floors(value: dict, answers: dict) -> dict:
    f = floors(answers)
    total = value.get("total_guaranteed_monthly")
    contractor = value["employment"] == "contractor"
    out = {"fte_floor": f["fte"], "contractor_floor": f["contractor"],
           "compared_against": None, "floor": None, "gap_monthly": None,
           "meets_floor": None, "warnings": []}
    if value["currency"] != "EUR":
        out["warnings"].append(f"offer is in {value['currency']}; floors are EUR. Convert before comparing.")
        return out
    if total is None:
        out["warnings"].append("no base pay found in the offer; nothing to compare.")
        return out
    if contractor:
        if f["contractor"] is not None:
            out["compared_against"], out["floor"] = "contractor_floor", f["contractor"]
        else:
            out["warnings"].append(
                "contractor offer but contractor_floor_eur_month is not set: unknown floor. "
                "A contractor number equal to the employee floor is a pay cut "
                "(taxes, benefits, leave and bench time are now his).")
            if f["fte"] is not None:
                implied = round_up(f["fte"] * CONTRACTOR_RULE_OF_THUMB)
                out["warnings"].append(
                    f"rule of thumb only (not a floor he set): {CONTRACTOR_RULE_OF_THUMB}x the "
                    f"FTE floor = {implied} EUR/month; this offer is "
                    f"{'below' if total < implied else 'at or above'} it.")
    elif f["fte"] is not None:
        out["compared_against"], out["floor"] = "fte_floor", f["fte"]
    else:
        out["warnings"].append("salary_fulltime_gross_eur_month is not set: unknown floor.")
    if out["floor"] is not None:
        out["gap_monthly"] = round(total - out["floor"], 2)
        out["meets_floor"] = total >= out["floor"]
    return out


_MH_PATTERNS = (
    (re.compile(r"remote\D*(\d+)\s*%"), "remote_pct", "min"),
    (re.compile(r"(full(?:y)?[- ]remote|100% remote|remote only|remote-only)"), "remote_pct", "min100"),
    (re.compile(r"notice\D*(\d+)\s*day"), "notice_days", "max"),
    (re.compile(r"(?:pto|vacation|holiday|leave)\D*(\d+)"), "pto_days", "min"),
    (re.compile(r"\b(employee|employment contract|no contractor)\b"), "employment", "employee"),
)


def check_must_haves(value: dict, answers: dict) -> list[dict]:
    """Each must-have -> {must_have, status: ok|violated|unknown|manual, detail}.

    A must-have is either a dict {field, min|max|equals} over the valued offer
    fields, or a short string matched by simple patterns ("remote 80%",
    "fully remote", "notice 30 days", "pto 25", "employee"). Anything else is
    returned as `manual`: check it by hand."""
    results = []
    for mh in (answers or {}).get("must_haves") or []:
        label = mh if isinstance(mh, str) else json.dumps(mh, ensure_ascii=False)
        rule = None
        if isinstance(mh, dict) and mh.get("field"):
            for op in ("min", "max", "equals"):
                if op in mh:
                    rule = (mh["field"], op, mh[op])
        elif isinstance(mh, str):
            low = mh.lower()
            for rx, field, op in _MH_PATTERNS:
                m = rx.search(low)
                if not m:
                    continue
                if op == "min100":
                    rule = (field, "min", 100)
                elif op == "employee":
                    rule = (field, "equals", "employee")
                else:
                    rule = (field, op, float(m.group(1)))
                break
        if rule is None:
            results.append({"must_have": label, "status": "manual", "detail": "check by hand"})
            continue
        field, op, want = rule
        have = value.get(field)
        if have is None or have == "unknown":
            status, detail = "unknown", f"offer does not state {field}"
        elif op == "min":
            status = "ok" if float(have) >= float(want) else "violated"
            detail = f"{field} {have:g} vs at least {float(want):g}"
        elif op == "max":
            status = "ok" if float(have) <= float(want) else "violated"
            detail = f"{field} {have:g} vs at most {float(want):g}"
        else:
            status = "ok" if str(have).lower() == str(want).lower() else "violated"
            detail = f"{field} {have} vs {want}"
        results.append({"must_have": label, "status": status, "detail": detail})
    return results


# --- justifications from the profile -----------------------------------------

_WORD_RE = re.compile(r"[a-z][a-z0-9+#.-]{2,}")
_STOP = {"the", "and", "for", "with", "from", "that", "this", "into", "role", "team",
         "work", "will", "our", "you", "your", "are", "per", "month", "year", "base"}


def profile_evidence(profile: dict) -> list[dict]:
    """Every bullet in the profile with its source id, verbatim."""
    out = []
    for kind in ("experiences", "projects"):
        for item in profile.get(kind) or []:
            for b in item.get("bullets") or []:
                if isinstance(b, str):
                    out.append({"source_id": item.get("id"), "text": b.strip(),
                                "where": item.get("company") or item.get("name") or ""})
    return out


def justifications(profile: dict, context: str = "", limit: int = 2) -> list[dict]:
    """The strongest 1-2 profile bullets for a counter: quantified ones first,
    then by word overlap with the offer/role text. Verbatim, never rewritten."""
    ctx = {w for w in _WORD_RE.findall((context or "").lower()) if w not in _STOP}
    scored = []
    for ev in profile_evidence(profile):
        words = set(_WORD_RE.findall(ev["text"].lower()))
        has_number = bool(re.search(r"\d", ev["text"]))
        scored.append((has_number, len(words & ctx), len(ev["text"]), ev))
    scored.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    picked, seen = [], set()
    for _, _, _, ev in scored:
        if ev["source_id"] in seen:
            continue
        picked.append(ev)
        seen.add(ev["source_id"])
        if len(picked) == limit:
            break
    return picked


def draft_counter(offer: dict, value: dict, comparison: dict, answers: dict,
                  profile: dict) -> dict:
    contractor = value["employment"] == "contractor"
    if contractor and floors(answers)["contractor"] is None:
        # Never fall back to the employee floor here: walking away at the FTE
        # number on a contractor offer is accepting the pay cut.
        return {"possible": False,
                "reason": "contractor offer and contractor_floor_eur_month is not set "
                          "(set it in answers.yaml, then re-run)",
                "text": None, "anchor": None, "fallback": None, "walk_away": None}
    reh = rehearsal(answers, contractor=contractor)
    total = value.get("total_guaranteed_monthly")
    if not reh["known"] or total is None or value["currency"] != "EUR":
        return {"possible": False, "reason": "floor unknown, base pay missing or non-EUR offer",
                "text": None, "anchor": None, "fallback": None, "walk_away": reh.get("walk_away")}
    anchor = max(reh["anchor"], round_up(total * 1.1))
    fallback = max(reh["fallback"], round_down((anchor + total) / 2), reh["walk_away"])
    if fallback >= anchor:
        fallback = max(reh["walk_away"], anchor - 100)
    context = " ".join(str(offer.get(k) or "") for k in ("role", "company")) + " " + \
        " ".join(offer.get("notes") or [])
    just = justifications(profile, context)
    company = offer.get("company") or "the team"
    unit = "EUR a month" + (" invoiced" if contractor else " gross")
    lines = [
        f"Thank you for the offer, I'm glad about how the conversations with {company} went "
        f"and I'd like to make this work.",
        "",
        f"Based on the scope of the role, I'd ask for {anchor:,} {unit}.",
    ]
    if just:
        lines.append("A couple of reasons I think that's fair:")
        lines += [f"- {j['text']}" for j in just]
    lines += [
        "",
        f"If that's out of reach, {fallback:,} {unit} would work for me"
        + (" with the rest of the package as offered." if not contractor else "."),
        "",
        f"I can't go below {reh['walk_away']:,} {unit}; if that isn't possible I'll "
        f"understand, and I'd be glad to stay in touch.",
    ]
    return {"possible": True, "anchor": anchor, "fallback": fallback,
            "walk_away": reh["walk_away"], "justifications": just, "text": "\n".join(lines)}


def evaluate(raw_offer: str, answers: dict, profile: dict) -> dict:
    offer = parse_offer(raw_offer)
    value = value_offer(offer)
    comparison = compare_to_floors(value, answers)
    must = check_must_haves(value, answers)
    counter = draft_counter(offer, value, comparison, answers, profile)
    return {"offer": offer, "value": value, "floors": comparison,
            "must_haves": must, "counter": counter}


def _money(v) -> str:
    return "missing" if v is None else f"{v:,.0f} EUR"


def to_markdown(result: dict) -> str:
    v, fl, c = result["value"], result["floors"], result["counter"]
    lines = ["# Offer evaluation", "",
             f"Employment: {v['employment']}  |  currency: {v['currency']}", "",
             "## Package per month", "",
             f"- Base: {_money(v['base_monthly'])}",
             f"- Guaranteed bonus: {_money(v['bonus_guaranteed_monthly'])}",
             f"- Target bonus (not guaranteed): {_money(v['bonus_target_monthly'])}",
             f"- Benefits: {_money(v['benefits_monthly'])}",
             f"- Equipment (over {EQUIPMENT_AMORTISE_MONTHS} months): {_money(v['equipment_monthly'])}",
             f"- **Guaranteed total: {_money(v['total_guaranteed_monthly'])}**",
             f"- With target bonus: {_money(v['total_with_target_monthly'])}",
             f"- Remote: {'missing' if v['remote_pct'] is None else format(v['remote_pct'], 'g') + '%'}",
             f"- PTO days: {'missing' if v['pto_days'] is None else format(v['pto_days'], 'g')}",
             f"- Notice days: {'missing' if v['notice_days'] is None else format(v['notice_days'], 'g')}",
             "", "## Against the floors", ""]
    if fl["floor"] is not None:
        verdict = "meets" if fl["meets_floor"] else "BELOW"
        lines.append(f"- {verdict} the {fl['compared_against'].replace('_', ' ')} "
                     f"({_money(fl['floor'])}); gap {fl['gap_monthly']:+,.0f} EUR/month")
    lines += [f"- {w}" for w in fl["warnings"]]
    if result["must_haves"]:
        lines += ["", "## Must-haves", ""]
        lines += [f"- [{m['status']}] {m['must_have']}: {m['detail']}" for m in result["must_haves"]]
    lines += ["", "## Counter draft", ""]
    if c["possible"]:
        lines += [f"Anchor {c['anchor']:,} / fallback {c['fallback']:,} / walk-away {c['walk_away']:,}",
                  "", c["text"]]
    else:
        lines.append(f"No counter drafted: {c['reason']}.")
    if v["missing"]:
        lines += ["", f"Ask them for: {', '.join(v['missing'])}."]
    lines += ["", "Never accept or decline on the call: \"I'll review it and come back by <date>.\""]
    return "\n".join(lines) + "\n"


def load_yaml(path: Path) -> dict:
    import yaml
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}
