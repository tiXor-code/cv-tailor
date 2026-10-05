"""Offer valuation against floors, must-haves and the counter draft.
Every number here is fictional."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from cv_tailor import offer_eval as oe

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {
    "experiences": [
        {"id": "acme", "company": "Acme Widgets",
         "bullets": ["Cut invoice processing from 9 days to 2 with an n8n pipeline.",
                     "Lead client delivery."]},
    ],
    "projects": [{"id": "botty", "name": "Botty",
                  "bullets": ["RAG support bot with 120 tests across 5 layers."]}],
}
ANSWERS = {"salary_fulltime_gross_eur_month": 3100,
           "must_haves": ["remote 80%", "notice 30 days", "a learning budget"]}


def test_package_valued_per_month():
    offer = {"base_annual": 39000, "bonus_guaranteed_annual": 1200, "bonus_target_pct": 10,
             "benefits_monthly": 50, "equipment_one_off": 2400, "remote_pct": 100,
             "pto_days": 25, "notice_days": 30, "employment": "employee"}
    v = oe.value_offer(offer)
    assert v["base_monthly"] == 3250
    assert v["bonus_guaranteed_monthly"] == 100
    assert v["bonus_target_monthly"] == 325
    assert v["equipment_monthly"] == 100
    assert v["total_guaranteed_monthly"] == 3500
    assert v["total_with_target_monthly"] == 3825
    assert v["missing"] == []


def test_thirteenth_salary_counts_as_guaranteed():
    v = oe.value_offer({"base_monthly": 3000, "months_per_year": 13})
    assert v["total_guaranteed_monthly"] == 3250


def test_below_fte_floor_is_flagged():
    v = oe.value_offer({"base_monthly": 3000, "employment": "employee"})
    c = oe.compare_to_floors(v, ANSWERS)
    assert c["meets_floor"] is False and c["gap_monthly"] == -100
    assert c["compared_against"] == "fte_floor"


def test_contractor_at_the_fte_number_is_a_pay_cut_when_floor_unknown():
    v = oe.value_offer({"base_monthly": 3100, "employment": "contractor"})
    c = oe.compare_to_floors(v, ANSWERS)
    assert c["floor"] is None and c["meets_floor"] is None
    text = " ".join(c["warnings"])
    assert "contractor_floor_eur_month is not set" in text and "pay cut" in text
    assert "4,100" not in text and "4100 EUR/month; this offer is below" in text


def test_no_counter_on_a_contractor_offer_without_a_contractor_floor():
    """Walking away at the FTE floor on a contractor offer would accept the pay cut."""
    r = oe.evaluate('{"base_monthly": 3300, "employment": "contractor"}', ANSWERS, PROFILE)
    assert r["counter"]["possible"] is False
    assert "contractor_floor_eur_month" in r["counter"]["reason"]


def test_full_remote_must_have():
    v = oe.value_offer({"base_monthly": 3500, "remote_pct": 80})
    assert oe.check_must_haves(v, {"must_haves": ["full remote"]})[0]["status"] == "violated"


def test_contractor_floor_used_when_set():
    v = oe.value_offer({"base_monthly": 4500, "employment": "contractor"})
    c = oe.compare_to_floors(v, {**ANSWERS, "contractor_floor_eur_month": 4800})
    assert c["compared_against"] == "contractor_floor" and c["meets_floor"] is False


def test_must_haves():
    v = oe.value_offer({"base_monthly": 3500, "remote_pct": 60, "notice_days": 30})
    res = {m["must_have"]: m["status"] for m in oe.check_must_haves(v, ANSWERS)}
    assert res == {"remote 80%": "violated", "notice 30 days": "ok",
                   "a learning budget": "manual"}
    res = oe.check_must_haves(v, {"must_haves": [{"field": "pto_days", "min": 20}]})
    assert res[0]["status"] == "unknown"


def test_text_offer_is_parsed():
    raw = ("Company: Fictco\nRole: AI Engineer\nBase salary: 42,000 EUR per year\n"
           "Bonus: 10% target\nMeal vouchers: 40 EUR/month\nLaptop: 1,200\n"
           "Remote: 80%\nVacation: 24 days\nNotice period: 1 month\nEmployment: full-time employee\n"
           "Free snacks")
    o = oe.parse_offer(raw)
    assert o["base_annual"] == 42000 and o["bonus_target_pct"] == 10
    assert o["benefits_monthly"] == 40 and o["equipment_one_off"] == 1200
    assert o["remote_pct"] == 80 and o["pto_days"] == 24 and o["notice_days"] == 30
    assert o["employment"] == "employee" and o["company"] == "Fictco"
    assert o["notes"] == ["Free snacks"]
    assert oe.parse_offer("Contract: B2B, 5300 EUR/month invoice")["employment"] == "contractor"


def test_non_eur_offer_is_not_compared():
    v = oe.value_offer({"base_monthly": 5300, "currency": "usd"})
    c = oe.compare_to_floors(v, ANSWERS)
    assert c["meets_floor"] is None and "USD" in c["warnings"][0]


def test_counter_uses_verbatim_profile_bullets_and_floor_as_walk_away():
    r = oe.evaluate(json.dumps({"base_monthly": 3000, "employment": "employee",
                                "company": "Fictco", "role": "automation engineer"}),
                    ANSWERS, PROFILE)
    c = r["counter"]
    assert c["possible"] and c["walk_away"] == 3100
    assert c["anchor"] > c["fallback"] >= c["walk_away"]
    assert "Cut invoice processing from 9 days to 2 with an n8n pipeline." in c["text"]
    assert "3,100" in c["text"]
    md = oe.to_markdown(r)
    assert "BELOW the fte floor" in md and "Ask them for: remote_pct, pto_days, notice_days" in md


def test_no_counter_without_a_floor():
    r = oe.evaluate('{"base_monthly": 3000}', {}, PROFILE)
    assert r["counter"]["possible"] is False
    assert "unknown floor" in " ".join(r["floors"]["warnings"])


def test_rehearsal_prefers_a_stated_target():
    r = oe.rehearsal({"salary_fulltime_gross_eur_month": 3100, "salary_target_eur_month": 3900})
    assert (r["anchor"], r["walk_away"], r["anchor_basis"]) == (3900, 3100, "salary_target_eur_month")
    assert 3100 <= r["fallback"] < 3900
    assert oe.rehearsal({}, contractor=True)["known"] is False


def test_script(tmp_path, monkeypatch, capsys):
    prof = tmp_path / "p.yaml"
    prof.write_text(yaml.safe_dump({"experiences": PROFILE["experiences"], "projects": []}))
    ans = tmp_path / "a.yaml"
    ans.write_text(yaml.safe_dump(ANSWERS))
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(prof))
    monkeypatch.setenv("CV_TAILOR_ANSWERS", str(ans))
    spec = importlib.util.spec_from_file_location("offer_eval_cli", ROOT / "scripts" / "offer_eval.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["offer_eval_cli"] = m
    spec.loader.exec_module(m)
    out_json = tmp_path / "o.json"
    assert m.main(["--out-json", str(out_json)], stdin=io.StringIO("Base: 3,400 EUR/month")) == 0
    assert json.loads(out_json.read_text())["value"]["base_monthly"] == 3400
    assert "# Offer evaluation" in capsys.readouterr().out
    assert m.main([], stdin=io.StringIO("")) == 2
    assert m.main([], stdin=io.StringIO("{not json")) == 2
