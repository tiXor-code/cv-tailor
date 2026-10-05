"""Listing fitness: fictional postings only (the repo is public)."""
from cv_tailor.vetting import BASELINE, band_for, red_flag_summary, vet_listing

HEALTHY_TECH = """
About us: Lumenfold is a 40-person B2B SaaS company in Utrecht building document
automation for logistics firms. We are profitable and remote-first across the EU.

The role: As an AI Automation Engineer you will design and ship LLM-powered
workflows that our customers run every day. You will report to the Head of Engineering.

Responsibilities: build and maintain agent pipelines with Python and TypeScript;
evaluate model output with our QA team; work with customers to turn messy processes
into reliable automations; keep an eye on cost and latency.

Requirements: 2+ years shipping software or automation in production; hands-on
experience with LLM APIs; clear written English. Nice to have: Claude Code, n8n.

What we offer: EUR 55,000 - 70,000 per year, 28 days of paid vacation, a learning
budget, health insurance and a home office budget.

Interview process: a 30-minute intro call, a take-home exercise, a technical interview
with two engineers, and a final interview with our CTO. We reply to every applicant
within 10 days.
"""

PLAIN_TECH_NO_PAY = """
Northwind Robotics is hiring a Solutions Engineer for our EMEA customers. You will
help customers integrate our fleet API, write example integrations, and feed product
feedback to the engineering team. We expect solid knowledge of REST APIs, Python,
and cloud basics (AWS or Azure). You will work closely with sales and support, travel
occasionally to customer sites in Germany and the Netherlands, and own technical
onboarding for new accounts. We are a team of 120 people with offices in Berlin and
Lisbon. Benefits include a pension plan, 30 days of holiday and a yearly conference
budget. Apply with your CV; we review applications weekly.
"""

SCAM = """
URGENT HIRING!!! Data entry assistant, work from home, NO EXPERIENCE NECESSARY, earn
$400 per day. Limited spots available, apply today before they are gone!!
To start, pay the registration fee of $49 for your starter kit. Send your full name,
social security number and bank account details to quickjobs.hiring2026@gmail.com
or contact us on WhatsApp at +1 555 010 2233. Be your own boss!
"""

DISCLAIMER = HEALTHY_TECH + """
Recruitment fraud notice: we will never ask you to pay a fee, buy equipment, or share
bank details or a copy of your passport during the hiring process, and we never
interview over WhatsApp or Telegram.
"""


def _ids(result, kind=None):
    return {f["id"] for f in result["flags"] if kind is None or f["kind"] == kind}


def test_healthy_tech_posting_scores_healthy_with_green_flags():
    r = vet_listing(HEALTHY_TECH)
    assert r["band"] == "healthy", r
    assert r["score"] >= 90
    assert not _ids(r, "red")
    assert {"stated_compensation", "structured_sections", "interview_process",
            "concrete_benefits", "reporting_line"} <= _ids(r, "green")


def test_plain_posting_without_pay_is_not_flagged_as_risky():
    r = vet_listing(PLAIN_TECH_NO_PAY)
    assert r["band"] in ("healthy", "mixed")
    assert r["score"] >= 75
    assert not _ids(r, "red")


def test_scam_posting_is_high_risk_with_evidence_for_each_flag():
    r = vet_listing(SCAM)
    assert r["band"] == "high_risk"
    assert r["score"] < 55
    reds = _ids(r, "red")
    for expected in ("payment_request", "sensitive_data_upfront", "chat_app_contact",
                     "free_mail_contact", "too_good_to_be_true", "mlm_commission_only",
                     "urgency_pressure", "shouting"):
        assert expected in reds, (expected, reds)
    for f in r["flags"]:
        assert f["evidence"], f
        assert (f["points"] < 0) == (f["kind"] == "red")


def test_anti_fraud_disclaimer_is_not_read_as_a_red_flag():
    r = vet_listing(DISCLAIMER)
    assert not _ids(r, "red"), r["flags"]
    assert r["band"] == "healthy"


def test_whatsapp_as_product_work_is_not_a_contact_flag():
    text = HEALTHY_TECH + "\nYou will build WhatsApp Business API and Telegram bot integrations."
    assert "chat_app_contact" not in _ids(vet_listing(text))


def test_thin_listing_and_wide_range_deduct_but_alone_stay_out_of_high_risk():
    r = vet_listing("Marketing lead wanted. Salary $30,000 - $150,000. Remote. Apply now.")
    assert {"thin_listing", "implausibly_wide_pay_range"} <= _ids(r, "red")
    assert r["band"] != "healthy"


def test_talent_pool_wording_is_flagged():
    r = vet_listing(PLAIN_TECH_NO_PAY + " Join our talent pool for future opportunities.")
    assert "evergreen_talent_pool" in _ids(r, "red")


def test_implausible_pay_is_too_good_to_be_true():
    r = vet_listing(PLAIN_TECH_NO_PAY + " Salary: EUR 90,000 per month.")
    assert "too_good_to_be_true" in _ids(r, "red")


def test_score_is_clamped_and_banded():
    r = vet_listing("")
    assert 0 <= r["score"] <= 100
    assert r["score"] == BASELINE - 15  # thin listing only
    assert band_for(80) == "healthy" and band_for(79) == "mixed"
    assert band_for(55) == "mixed" and band_for(54) == "high_risk"


def test_each_flag_counts_once():
    r = vet_listing(SCAM + SCAM)
    ids = [f["id"] for f in r["flags"]]
    assert len(ids) == len(set(ids))


def test_red_flag_summary_is_readable():
    assert "payment request" in red_flag_summary(vet_listing(SCAM))
    assert red_flag_summary({"flags": []}) == "no single red flag"
