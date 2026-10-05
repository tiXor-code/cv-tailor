"""Where vetting hooks into Scout: scan -> queue entry, autopilot, the apply
orchestrator, listing_enrich and the backfill script. Fictional data only; the
LLM and SerpAPI are faked."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cv_tailor import listing_enrich
from cv_tailor.autopilot import HOLD_HIGH_RISK, build_digest, run_autopilot
from cv_tailor.scout_queue import _to_entry, update_entry
from cv_tailor.vetting import vet_listing

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
TODAY = "2026-10-05"
FLOORS = {"fte": 3100, "contractor": None}

SCAM = ("URGENT HIRING!!! Remote assistant, NO EXPERIENCE NECESSARY, earn $400 per day. "
        "Pay the registration fee of $49 for your starter kit, then send your social security "
        "number and bank account details to quickjobs.hiring2026@gmail.com or message us on "
        "WhatsApp at +1 555 010 2233. Limited spots available!!")
HEALTHY = ("About us: Lumenfold builds document automation. The role: ship LLM workflows and "
           "report to the Head of Engineering. Responsibilities: build agent pipelines, evaluate "
           "output, work with customers. Requirements: production automation experience. What we "
           "offer: EUR 55,000 - 70,000 per year, 28 days of paid vacation, health insurance. "
           "Interview process: intro call, take-home, technical interview, final interview.")


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(self.reply)))])


def _write_day(root: Path, day: str, entries: list[dict], descriptions: dict | None = None):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps(entries))
    if descriptions is not None:
        (d / "descriptions.json").write_text(json.dumps(descriptions))


def _entry(i, score=8, status="pending", **over):
    base = {"id": f"job-{i}", "title": f"Role {i}", "company": f"Co{i}", "location": "Remote, EU",
            "score": score, "status": status, "decided_at": None, "apply_method": "portal",
            "apply_target": f"https://example.invalid/{i}", "url": f"https://example.invalid/{i}"}
    base.update(over)
    return base


def _read(root, day=TODAY):
    return {e["id"]: e for e in json.loads((root / day / "jobs.json").read_text())}


# --- scan -> queue entry ---------------------------------------------------------

def test_queue_entry_carries_vetting_only_when_computed():
    job = SimpleNamespace(source="ashby", raw_id="1", url="https://example.invalid/1", title="T",
                          org="Lumenfold", location="Remote", description=HEALTHY, apply_options=[])
    plain = _to_entry({"job": job, "score": 8})
    assert "listing_fitness" not in plain and "pay" not in plain
    fields = listing_enrich.scan_fields(HEALTHY, FLOORS)
    vetted = _to_entry({"job": job, "score": 8, **fields})
    assert vetted["listing_fitness"]["band"] == "healthy"
    assert vetted["pay"]["stated"][0]["currency"] == "EUR"
    assert vetted["pay"]["verdict"] == "apply"
    json.dumps(vetted)  # the admin UI reads it as JSON


def test_scan_attach_vetting_marks_every_item():
    sys.path.insert(0, str(ROOT / "scripts"))
    import scan
    items = [{"job": SimpleNamespace(description=d, org="Co"), "score": 8} for d in (HEALTHY, SCAM)]
    scan.attach_vetting(items, floors=FLOORS)
    assert [i["listing_fitness"]["band"] for i in items] == ["healthy", "high_risk"]
    assert all("pay" in i for i in items)


# --- autopilot ------------------------------------------------------------------

def _runner_sends(root, ran):
    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=root)
        return 0
    return runner


def test_high_risk_listing_below_9_is_rejected_unapplied_with_reason(tmp_path):
    _write_day(tmp_path, TODAY, [
        _entry(1, score=8, listing_fitness=vet_listing(SCAM)),
        _entry(2, score=8, listing_fitness=vet_listing(HEALTHY)),
    ])
    ran = []
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=_runner_sends(tmp_path, ran))
    assert ran == ["job-2"]
    q = _read(tmp_path)
    assert q["job-1"]["status"] == "rejected"
    assert q["job-1"]["error"].startswith("high_risk_listing: fitness ")
    assert q["job-1"]["decided_by"] == "autopilot"
    assert [e["id"] for _, e in report.risky] == ["job-1"]
    assert "Not applied: high-risk listing" in build_digest(report)


def test_high_risk_listing_scoring_9_is_approved_with_a_hold(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9, listing_fitness=vet_listing(SCAM))])
    seen = []

    def runner(day, job_id):
        seen.append(_read(tmp_path)[job_id].get("apply_hold"))
        update_entry(day, job_id, lambda e: e.update(status="handed_off"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert seen == [HOLD_HIGH_RISK]
    assert [e["id"] for _, e in report.handed_off] == ["job-1"]


def test_entries_queued_before_vetting_are_vetted_from_their_description(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=7)], descriptions={"job-1": SCAM})
    ran = []
    run_autopilot(NOW, queue_dir=tmp_path, runner=_runner_sends(tmp_path, ran))
    q = _read(tmp_path)
    assert ran == []
    assert q["job-1"]["status"] == "rejected"
    assert q["job-1"]["listing_fitness"]["band"] == "high_risk"


def test_missing_description_is_unknown_not_high_risk(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=7)])
    ran = []
    run_autopilot(NOW, queue_dir=tmp_path, runner=_runner_sends(tmp_path, ran))
    assert ran == ["job-1"]


# --- apply orchestrator ---------------------------------------------------------

def _load_apply():
    spec = importlib.util.spec_from_file_location("apply_approved_vetting",
                                                  ROOT / "scripts" / "apply_approved.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def held_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    _write_day(tmp_path, TODAY, [_entry(1, score=9, status="approved", approved_by="autopilot",
                                        apply_hold=HOLD_HIGH_RISK,
                                        listing_fitness=vet_listing(SCAM))],
               descriptions={"job-1": SCAM})
    return tmp_path


def test_held_job_is_assembled_and_handed_over_never_submitted(held_queue, monkeypatch):
    mod = _load_apply()
    mod.assemble_package = lambda entry, day: {"package_dir": "/tmp/pkg", "cv_path": "/tmp/pkg/cv.pdf",
                                               "cover_letter_path": "/tmp/pkg/cl.txt",
                                               "cover_letter_warnings": []}
    mod.load_profile = lambda *a, **k: {"name": "Fake"}
    mod.load_answers = lambda *a, **k: {}
    mod.linkedin_handoff.answer_sheet = lambda p, a: "sheet"

    def _never(*a, **k):
        raise AssertionError("a held job must never reach a submit path")

    mod._handle_portal = _never
    mod.send_application = _never
    listed, approved = [], []
    monkeypatch.setattr(mod.listing_enrich, "enrich_listed", lambda d, j, **k: listed.append(j))
    monkeypatch.setattr(mod.listing_enrich, "enrich_approved", lambda d, j, **k: approved.append(j))

    assert mod.main([TODAY, "job-1"]) == 0
    e = _read(held_queue)["job-1"]
    assert e["status"] == "handed_off"
    assert e["listing_warning"].startswith("High-risk listing (fitness ")
    assert "did not apply" in e["handoff_reason"]
    assert e["cv_path"] == "/tmp/pkg/cv.pdf"
    assert approved == ["job-1"] and listed == ["job-1"]


# --- listing_enrich -------------------------------------------------------------

@pytest.fixture
def live_enrich(monkeypatch):
    monkeypatch.delenv(listing_enrich.DISABLED_ENV, raising=False)


def test_enrich_pay_estimates_once_and_stores(tmp_path, live_enrich):
    _write_day(tmp_path, TODAY, [_entry(1, status="approved")],
               descriptions={"job-1": "Build agents for logistics customers. Remote EU startup."})
    client = FakeClient({"min_eur_month": 3500, "max_eur_month": 4500, "basis": "Mid-level, EU remote."})
    listing_enrich.enrich_pay(TODAY, "job-1", queue_dir=tmp_path, client=client, floors=FLOORS)
    listing_enrich.enrich_pay(TODAY, "job-1", queue_dir=tmp_path, client=client, floors=FLOORS)
    e = _read(tmp_path)["job-1"]
    assert len(client.calls) == 1
    assert e["pay"]["estimate"] == {"min_eur_month": 3500, "max_eur_month": 4500}
    assert e["pay"]["verdict"] == "apply"
    assert e["status"] == "approved"  # status-neutral
    assert "listing_fitness" in e


def test_enrich_is_off_when_disabled(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, status="handed_off", score=9)], descriptions={"job-1": HEALTHY})
    client = FakeClient({})
    listing_enrich.enrich_listed(TODAY, "job-1", queue_dir=tmp_path, client=client)
    assert client.calls == []
    assert "company_check" not in _read(tmp_path)["job-1"]


def test_company_check_only_for_score_8_plus_and_stored(tmp_path, live_enrich):
    _write_day(tmp_path, TODAY, [_entry(1, status="handed_off", score=7),
                                 _entry(2, status="handed_off", score=8)])
    searches = []

    def search(params):
        searches.append(params)
        return {"organic_results": [{"link": "https://co2.example/careers", "title": "Role 2"}]}

    client = FakeClient({"verdict": "proceed", "summary": "Fine.", "evidence": ["https://co2.example/careers"]})
    rec1, note1 = listing_enrich.enrich_company(TODAY, "job-1", queue_dir=tmp_path, client=client, search=search)
    rec2, note2 = listing_enrich.enrich_company(TODAY, "job-2", queue_dir=tmp_path, client=client, search=search)
    assert (rec1, note1) == (None, "below-score")
    assert note2 == "checked"
    stored = _read(tmp_path)["job-2"]["company_check"]
    assert set(stored) == {"verdict", "summary", "evidence", "checked_at", "own_site_lists_role"}
    assert stored["evidence"] == ["https://co2.example/careers"]
    assert len(searches) == 3
    assert (tmp_path / "vetting" / "serpapi_budget.json").exists()


def test_listing_warning_only_for_high_risk(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, status="handed_off", listing_fitness=vet_listing(SCAM)),
                                 _entry(2, status="handed_off", listing_fitness=vet_listing(HEALTHY))])
    assert listing_enrich.mark_listing_warning(TODAY, "job-1", queue_dir=tmp_path)
    assert listing_enrich.mark_listing_warning(TODAY, "job-2", queue_dir=tmp_path) is None
    q = _read(tmp_path)
    assert "listing_warning" in q["job-1"] and "listing_warning" not in q["job-2"]


def test_enrich_never_raises_on_a_missing_queue(tmp_path, live_enrich):
    assert listing_enrich.enrich_pay(TODAY, "nope", queue_dir=tmp_path) is None
    assert listing_enrich.enrich_company(TODAY, "nope", queue_dir=tmp_path) == (None, "no-entry")


# --- backfill -------------------------------------------------------------------

def _load_backfill():
    spec = importlib.util.spec_from_file_location("backfill_vetting", ROOT / "scripts" / "backfill_vetting.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def listed_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    from datetime import date
    today = date.today().isoformat()
    _write_day(tmp_path, today, [
        _entry(1, status="handed_off", score=9),
        _entry(2, status="handed_off", score=6),
        _entry(3, status="sent", score=9),
    ], descriptions={"job-1": SCAM, "job-2": "Build agents. Remote EU.", "job-3": HEALTHY})
    return tmp_path, today


def test_backfill_dry_run_writes_nothing(listed_queue, capsys):
    root, today = listed_queue
    before = (root / today / "jobs.json").read_text()
    assert _load_backfill().main(["--dry-run"]) == 0
    assert (root / today / "jobs.json").read_text() == before
    assert not (root / "vetting").exists()
    out = capsys.readouterr().out
    assert "2 job(s) on his list" in out
    assert "would use up to 3 company-check search(es)" in out


def test_backfill_enriches_only_his_list_within_budget(listed_queue, monkeypatch, live_enrich):
    root, today = listed_queue
    monkeypatch.setenv("SCOUT_COMPANY_CHECK_DAILY_CAP", "3")
    client = FakeClient({"min_eur_month": 2000, "max_eur_month": 2600, "basis": "Junior role.",
                         "verdict": "avoid", "summary": "Scam reports.", "evidence": []})
    searches = []
    rc = _load_backfill().main([], client=client,
                               search=lambda p: searches.append(p) or {"organic_results": []})
    assert rc == 0
    q = _read(root, today)
    assert q["job-1"]["listing_fitness"]["band"] == "high_risk"
    assert q["job-1"]["listing_warning"].startswith("High-risk listing")
    assert q["job-1"]["status"] == "handed_off"  # never moved off his list
    assert q["job-1"]["company_check"]["verdict"] == "avoid"
    assert "company_check" not in q["job-2"]  # score 6
    assert q["job-2"]["pay"]["verdict"] == "skip: below floor" or q["job-2"]["pay"]["verdict"] is None
    assert "pay" not in q["job-3"]  # not on his list
    assert len(searches) == 3
