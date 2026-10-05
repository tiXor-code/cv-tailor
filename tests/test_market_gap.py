"""scripts/market_gap.py -- document-frequency ranking over every
descriptions.json in the Scout queue. Fictional postings, tmp queue only."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import market_gap  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ats"


def _seed(root: Path):
    (root / "2026-01-10").mkdir(parents=True)
    (root / "2026-01-10" / "descriptions.json").write_text(json.dumps({
        "a": "Build RAG systems on Kubernetes. Python services and APIs.",
        "b": "Kubernetes operators in Go. Kubernetes clusters at scale.",
    }))
    (root / "2026-01-11").mkdir()
    (root / "2026-01-11" / "descriptions.json").write_text(json.dumps({
        "c": "Python data pipelines. An API for RAG search.",
        "a": "Build RAG systems on Kubernetes. Python services and APIs.",  # repeat id
    }))
    (root / "not-a-day").mkdir()
    (root / "not-a-day" / "descriptions.json").write_text(json.dumps({"z": "Cobol Cobol"}))


def test_load_descriptions_dedupes_ids_and_skips_non_day_dirs(tmp_path):
    _seed(tmp_path)
    descs = market_gap.load_descriptions(tmp_path)
    assert sorted(descs) == ["a", "b", "c"]


def test_ranks_by_document_frequency_and_flags_unsupported(tmp_path):
    _seed(tmp_path)
    profile = {"bio": "Ships Python services and a retrieval-augmented generation API."}
    report = market_gap.market_gap(market_gap.load_descriptions(tmp_path), profile, top_n=10)
    by_term = {r["term"].lower(): r for r in report["top_terms"]}
    assert report["postings"] == 3
    assert by_term["kubernetes"]["postings"] == 2   # counted once per posting, not 3x
    assert by_term["kubernetes"]["supported"] is False
    assert by_term["rag"]["supported"] is True      # via retrieval-augmented generation
    api = [r for t, r in by_term.items() if t in ("api", "apis")]
    assert len(api) == 1 and api[0]["postings"] == 2  # "APIs" and "API" are one term
    assert "kubernetes" in [t.lower() for t in report["unsupported"]]
    assert "cobol" not in by_term


def test_main_writes_json_to_out(tmp_path, monkeypatch, capsys):
    _seed(tmp_path / "q")
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path / "q"))
    out = tmp_path / "report.json"
    rc = market_gap.main(["--top", "5", "--out", str(out), "--profile", str(FIX / "profile_ats.yaml")])
    assert rc == 0
    data = json.loads(out.read_text())
    assert len(data["top_terms"]) == 5
    assert "3 postings analysed" in capsys.readouterr().out


def test_main_defaults_output_under_queue_root(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    assert market_gap.main(["--profile", str(FIX / "profile_ats.yaml")]) == 0
    assert (tmp_path / "market_gap.json").exists()


def test_main_empty_queue_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    assert market_gap.main([]) == 1
