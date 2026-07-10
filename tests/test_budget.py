import json
from datetime import date

from cv_tailor.budget import SerpBudget


def test_take_decrements_available_budget(tmp_path):
    b = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=3)
    assert b.used() == 0
    assert b.take() is True
    assert b.used() == 1
    assert b.take() is True
    assert b.used() == 2


def test_take_returns_false_when_exhausted(tmp_path):
    b = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=2)
    assert b.take() is True
    assert b.take() is True
    assert b.take() is False
    assert b.take() is False
    # a blocked take() must NOT bump the stored count past the cap
    assert b.used() == 2


def test_month_rollover_resets_used(tmp_path):
    path = tmp_path / "serpapi_budget.json"
    path.write_text(json.dumps({"month": "2020-01", "used": 90}))
    b = SerpBudget(path=path, monthly_cap=90)
    assert b.used() == 0
    assert b.take() is True
    assert b.used() == 1
    on_disk = json.loads(path.read_text())
    assert on_disk["month"] == date.today().strftime("%Y-%m")
    assert on_disk["used"] == 1


def test_atomicity_no_tmp_residue(tmp_path):
    path = tmp_path / "serpapi_budget.json"
    b = SerpBudget(path=path, monthly_cap=90)
    for _ in range(5):
        b.take()
    leftovers = list(tmp_path.glob("*.tmp-*"))
    assert leftovers == []
    assert json.loads(path.read_text())["used"] == 5


def test_missing_file_starts_at_zero(tmp_path):
    b = SerpBudget(path=tmp_path / "nope" / "serpapi_budget.json", monthly_cap=90)
    assert b.used() == 0


def test_corrupt_file_treated_as_fresh_month(tmp_path):
    path = tmp_path / "serpapi_budget.json"
    path.write_text("not json")
    b = SerpBudget(path=path, monthly_cap=90)
    assert b.used() == 0
    assert b.take() is True
    assert b.used() == 1


def test_default_path_and_cap():
    b = SerpBudget()
    assert b.monthly_cap == 90
    assert b.path.name == "serpapi_budget.json"
    assert b.path.parent.name == "data"
