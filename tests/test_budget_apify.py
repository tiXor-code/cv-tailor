"""ApifyResultBudget: metered by RESULT, not by request.

SerpBudget and JSearchBudget count requests because those APIs price per
request. cheap_scraper/linkedin-job-scraper prices per ROW, so one run that
returns 200 rows costs 200 units while a request counter would report 1. On a
FREE tier with $5/month that difference is the whole ballgame.
"""
import json
from datetime import date, timedelta

from cv_tailor.budget import ApifyResultBudget


def test_reserve_grants_only_up_to_the_monthly_headroom(tmp_path):
    b = ApifyResultBudget(path=tmp_path / "apify_result_budget.json", monthly_cap=10)
    assert b.reserve(4) == 4
    assert b.used() == 4
    assert b.reserve(10) == 6, "clamped to what is left, not what was asked for"
    assert b.used() == 10
    assert b.reserve(1) == 0


def test_settle_refunds_the_unused_grant(tmp_path):
    """Reserve-then-settle fails CLOSED. The grant is charged up front, so a
    crash or client timeout between the POST and the settle leaves the worst
    case charged. Charging after the fact would under-count in exactly the
    situation where the run was most expensive."""
    b = ApifyResultBudget(path=tmp_path / "a.json", monthly_cap=100)
    granted = b.reserve(25)
    assert granted == 25
    assert b.used() == 25, "the full grant is held while the run is in flight"
    b.settle(granted, 12)
    assert b.used() == 12


def test_settle_never_refunds_more_than_was_granted(tmp_path):
    """If the actor somehow returns more rows than were granted, the refund
    must not go negative and hand back allowance that was never reserved."""
    b = ApifyResultBudget(path=tmp_path / "a.json", monthly_cap=100)
    granted = b.reserve(5)
    b.settle(granted, 9)
    assert b.used() == 5


def test_a_daily_cap_spreads_spend_across_the_month(tmp_path):
    """Without the daily leg the whole monthly allowance is gone by day 8."""
    b = ApifyResultBudget(path=tmp_path / "a.json", monthly_cap=600, daily_cap=25)
    assert b.reserve(20) == 20
    assert b.reserve(20) == 5, "daily headroom only"
    assert b.reserve(1) == 0, "monthly headroom remains, daily does not"


def test_the_daily_counter_resets_on_a_new_day_and_the_monthly_one_does_not(tmp_path):
    path = tmp_path / "a.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({
        "month": date.today().strftime("%Y-%m"),
        "used": 40, "day": yesterday, "day_used": 25,
    }))
    b = ApifyResultBudget(path=path, monthly_cap=600, daily_cap=25)
    assert b.used() == 40, "the month is not over"
    assert b.reserve(10) == 10, "but the day is"


def test_it_has_its_own_counter_file_and_label(tmp_path):
    """A shared counter file lets one source spend another's allowance, which
    is the stated reason serpapi and jsearch were split in the first place."""
    assert ApifyResultBudget.DEFAULT_FILENAME == "apify_result_budget.json"
    assert ApifyResultBudget.LABEL == "apify"
    assert ApifyResultBudget.DEFAULT_FILENAME not in (
        "serpapi_budget.json", "jsearch_budget.json", "budget.json")


def test_take_still_behaves_like_every_other_budget(tmp_path):
    """take() is inherited and must stay byte-compatible, so no existing
    budget test or caller changes behaviour."""
    b = ApifyResultBudget(path=tmp_path / "a.json", monthly_cap=2)
    assert b.take() is True
    assert b.take() is True
    assert b.take() is False
    assert b.used() == 2


def test_a_zero_cap_grants_nothing(tmp_path):
    """--dry-run builds the budget with monthly_cap=0, so a dry run is free BY
    CONSTRUCTION rather than by someone remembering to skip the source."""
    b = ApifyResultBudget(path=tmp_path / "a.json", monthly_cap=0)
    assert b.reserve(50) == 0
    assert b.used() == 0


def test_month_rollover_resets_both_counters(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({
        "month": "2020-01", "used": 600,
        "day": "2020-01-31", "day_used": 25,
    }))
    b = ApifyResultBudget(path=path, monthly_cap=600, daily_cap=25)
    assert b.used() == 0
    assert b.reserve(5) == 5
    on_disk = json.loads(path.read_text())
    assert on_disk["month"] == date.today().strftime("%Y-%m")
    assert on_disk["day"] == date.today().isoformat()
