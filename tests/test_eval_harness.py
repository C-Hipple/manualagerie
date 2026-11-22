"""The eval harness itself: retrieval mode against fixtures. No network, no LLM."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.test_ingest import _ingest

_spec = importlib.util.spec_from_file_location("eval_script", Path(__file__).parent.parent / "scripts" / "eval.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

YAML = """
questions:
  - question: What size is the drain hose connection?
    manual: dishwasher
    pages: [3]
  - question: What size is the drain hose connection?
    manual: dishwasher
    pages: "5-6"
  - question: water supply compression fitting
    manual: dishwasher.pdf
  - question: What is the printer wifi password?
    expect_not_found: true
  - question: drain hose
    manual: no-such-manual
"""


def test_parse_pages():
    assert ev.parse_pages(None) == []
    assert ev.parse_pages(4) == [(4, 4)]
    assert ev.parse_pages([2, "5-7"]) == [(2, 2), (5, 7)]
    assert ev.overlaps([(5, 7)], 7, 8) and not ev.overlaps([(5, 7)], 8, 9)
    with pytest.raises(ValueError):
        ev.parse_pages("abc")


def test_says_not_found():
    assert ev.says_not_found("The manuals don't say what the Wi-Fi password is.")
    assert ev.says_not_found("I couldn't find that in your manuals.")
    assert not ev.says_not_found("The drain hose fits 5/8 inch [#1 p.3].")


def test_load_questions_validation(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("questions:\n  - question: hi\n")
    with pytest.raises(ValueError):
        ev.load_questions(bad)
    assert len(ev.load_questions(ev.DEFAULT_QUESTIONS)) >= 6


def test_retrieval_mode(conn, pdf_path, tmp_path):
    _ingest(conn, pdf_path)
    f = tmp_path / "q.yaml"
    f.write_text(YAML)
    report = ev.run_eval(ev.load_questions(f), retrieval_only=True, conn=conn)
    statuses = [r["retrieval"]["status"] for r in report["results"]]
    assert statuses == ["hit", "miss", "hit", "skip", "error"]
    s = report["summary"]
    assert s["retrieval_checked"] == 4 and s["retrieval_hits"] == 2 and s["retrieval_hit_rate"] == 0.5
    assert "answer_checked" not in s
    text = ev.format_report(report, retrieval_only=True)
    assert "HIT" in text and "MISS" in text and "50%" in text


def test_answer_checks(conn, pdf_path):
    mid = _ingest(conn, pdf_path)
    q = {"question": "x", "manual": "dishwasher", "pages": [3], "answer_must_contain": ["5/8"]}
    cite = [{"manual_id": mid, "page": 3, "page_end": 3}]
    assert ev.check_answer(conn, q, "Fits 5/8 inch", cite)["status"] == "pass"
    assert "missing" in ev.check_answer(conn, q, "Fits 1 inch", cite)["detail"]
    wrong = [{"manual_id": mid, "page": 1, "page_end": 1}]
    assert "citations" in ev.check_answer(conn, q, "5/8", wrong)["detail"]
    nf = {"question": "x", "expect_not_found": True}
    assert ev.check_answer(conn, nf, "It isn't covered in the manuals.", [])["status"] == "pass"
    assert ev.check_answer(conn, nf, "It is 1234.", [])["status"] == "fail"


def test_write_results(tmp_path):
    report = {"results": [], "summary": {}, "k": 6}
    path = ev.write_results(report, tmp_path)
    assert path.parent == tmp_path / "evals" and json.loads(path.read_text())["k"] == 6
