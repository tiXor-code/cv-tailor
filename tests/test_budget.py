import json
import multiprocessing
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


def _take_n_times(path, monthly_cap, n, queue):
    """Module-level so multiprocessing (spawn) can pickle/import it as the
    child process target. Each call is its own full read-modify-write cycle
    through take(), exactly like two real cv-tailor/norina-jobs processes
    racing to consume the same shared monthly SerpAPI budget."""
    from cv_tailor.budget import SerpBudget

    b = SerpBudget(path=path, monthly_cap=monthly_cap)
    successes = sum(1 for _ in range(n) if b.take())
    queue.put(successes)


def test_take_concurrent_writers_no_lost_updates_no_overshoot(tmp_path):
    """Regression test for the plain read-modify-write race: two real OS
    processes (not threads) each call take() 30 times against a shared cap
    of 40. Without a lock held across the whole read-compare-increment-write,
    both processes can read the same `used` value, both pass the cap check,
    and both write -- overshooting the cap and/or losing one writer's
    increment. With the flock in place, exactly `cap` calls succeed in total
    across both processes and the stored `used` matches exactly (mirrors
    tests/test_scout_queue.py's concurrent-writers test)."""
    path = tmp_path / "serpapi_budget.json"
    n = 30
    cap = 40
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    p1 = ctx.Process(target=_take_n_times, args=(path, cap, n, queue))
    p2 = ctx.Process(target=_take_n_times, args=(path, cap, n, queue))
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)

    assert p1.exitcode == 0, "worker 1 crashed (see traceback above)"
    assert p2.exitcode == 0, "worker 2 crashed (see traceback above)"

    total_successes = queue.get(timeout=5) + queue.get(timeout=5)
    assert total_successes == cap, "overshoot or lost update in take()'s successful-call count"
    assert json.loads(path.read_text())["used"] == cap, "stored used count diverged from successes"
