"""Evaluation harness: measures retrieval hit rate and (optionally) end-to-end answer quality.

    uv run python scripts/eval.py [--retrieval-only] [--limit N] [--k N] [--file PATH]

Uses the real DATA_DIR database. Retrieval needs no LLM. Answer checks run the real agent
(needs OPENROUTER_API_KEY) in throwaway conversations that are deleted afterwards.
Results are printed and written to {DATA_DIR}/evals/eval-<timestamp>.json.
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.repo import manuals as manuals_repo  # noqa: E402
from app.search import fts  # noqa: E402

DEFAULT_QUESTIONS = ROOT / "tests" / "evals" / "questions.yaml"
DEFAULT_K = 6  # matches the search_manuals tool's default limit

NOT_FOUND_PATTERNS = [
    r"\bnot (?:found|covered|mentioned|specified|listed|included|provided)\b",
    r"\b(?:can(?:no|')?t|could(?: not|n't)|unable to|did(?: not|n't)) (?:find|locate|see)\b",
    r"\b(?:do(?:es)?(?: not|n't)|doesn't|don't) (?:say|state|specify|mention|contain|include|cover|provide|list|have|address)\b",
    r"\bno (?:information|mention|details?|reference|passages?|instructions?|specification)\b",
    r"\bnot (?:something )?(?:in|addressed|available)\b",
    r"\bisn't (?:covered|mentioned|in)\b",
]
_not_found_re = re.compile("|".join(NOT_FOUND_PATTERNS), re.IGNORECASE)


def parse_pages(spec) -> list[tuple[int, int]]:
    """Normalise `pages` (int, 'a-b', or a list of those) into inclusive (start, end) ranges."""
    if spec is None:
        return []
    items = spec if isinstance(spec, list) else [spec]
    out = []
    for it in items:
        if isinstance(it, int):
            out.append((it, it))
            continue
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*", str(it))
        if not m:
            raise ValueError(f"bad pages value: {it!r}")
        a = int(m.group(1))
        out.append((a, int(m.group(2) or a)))
    return out


def overlaps(ranges: list[tuple[int, int]], a: int, b: int) -> bool:
    return any(a <= e and s <= b for s, e in ranges)


def load_questions(path: Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    qs = data.get("questions", data) if isinstance(data, dict) else data
    if not isinstance(qs, list):
        raise ValueError("questions file must contain a list under `questions:`")
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict) or not q.get("question"):
            raise ValueError(f"entry {i}: `question` is required")
        if not q.get("expect_not_found") and not q.get("manual"):
            raise ValueError(f"entry {i}: `manual` is required unless expect_not_found is true")
        parse_pages(q.get("pages"))
    return qs


def resolve_manual_ids(conn, needle: str | None) -> set[int]:
    if not needle:
        return set()
    n = needle.lower()
    return {r["id"] for r in manuals_repo.list_all(conn)
            if n in (r["title"] or "").lower() or n in (r["filename"] or "").lower()}


def check_retrieval(conn, q: dict, k: int) -> dict:
    if q.get("expect_not_found"):
        return {"status": "skip", "detail": "expect_not_found"}
    ids = resolve_manual_ids(conn, q["manual"])
    if not ids:
        return {"status": "error", "detail": f"no manual matches {q['manual']!r}"}
    pages = parse_pages(q.get("pages"))
    hits = fts.search(conn, q["question"], None, k, get_settings().preferred_lang_list)
    for rank, h in enumerate(hits, 1):
        if h["manual_id"] in ids and (not pages or overlaps(pages, h["page_start"], h["page_end"])):
            return {"status": "hit", "rank": rank, "detail": f"rank {rank}"}
    got = [f"#{h['manual_id']} p.{h['page_start']}-{h['page_end']}" for h in hits[:3]]
    return {"status": "miss", "rank": None, "detail": "top: " + (", ".join(got) or "no results")}


def says_not_found(text: str) -> bool:
    return bool(_not_found_re.search(text or ""))


def check_answer(conn, q: dict, answer: str, citations: list[dict]) -> dict:
    problems = []
    if q.get("expect_not_found"):
        if not says_not_found(answer):
            problems.append("did not say the answer was not found")
    else:
        ids = resolve_manual_ids(conn, q["manual"])
        pages = parse_pages(q.get("pages"))
        ok = any(c["manual_id"] in ids and (not pages or overlaps(pages, c["page"], c["page_end"]))
                 for c in citations)
        if not ok:
            problems.append("citations miss expected manual/page")
    for s in q.get("answer_must_contain") or []:
        if str(s).lower() not in (answer or "").lower():
            problems.append(f"missing {s!r}")
    return {"status": "pass" if not problems else "fail", "detail": "; ".join(problems)}


async def run_agent(question: str) -> tuple[str, list[dict], str | None]:
    """Run the real agent headlessly in a throwaway conversation. Returns (answer, citations, error)."""
    from app.llm import agent
    from app.repo import chat as chat_repo

    conn = db.connect()
    try:
        cid = chat_repo.create_conversation(conn, "eval")
        conn.commit()
    finally:
        conn.close()
    answer, citations, error = "", [], None
    try:
        async for ev in agent.run_turn(cid, question):
            if ev["type"] == "error":
                error = ev["text"]
            elif ev["type"] == "done":
                citations = ev["citations"]
    finally:
        conn = db.connect()
        try:
            msgs = [m for m in chat_repo.get_messages(conn, cid) if m["role"] == "assistant" and m["content"]]
            if msgs:
                answer = msgs[-1]["content"]
            chat_repo.delete_conversation(conn, cid)
            conn.commit()
        finally:
            conn.close()
    return answer, citations, error


def run_eval(questions: list[dict], *, retrieval_only: bool, k: int = DEFAULT_K, conn=None) -> dict:
    own = conn is None
    conn = conn or db.connect()
    results = []
    try:
        for q in questions:
            r = {"question": q["question"], "expect_not_found": bool(q.get("expect_not_found")),
                 "retrieval": check_retrieval(conn, q, k)}
            if not retrieval_only:
                if get_settings().openrouter_api_key:
                    answer, citations, error = asyncio.run(run_agent(q["question"]))
                    r["answer"] = answer
                    r["citations"] = citations
                    r["answer_check"] = ({"status": "error", "detail": error} if error
                                         else check_answer(conn, q, answer, citations))
                else:
                    r["answer_check"] = {"status": "error", "detail": "OPENROUTER_API_KEY not set"}
            results.append(r)
    finally:
        if own:
            conn.close()
    return {"results": results, "summary": summarize(results, retrieval_only), "k": k}


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def summarize(results: list[dict], retrieval_only: bool) -> dict:
    ret = [r["retrieval"]["status"] for r in results if r["retrieval"]["status"] != "skip"]
    s = {"total": len(results), "retrieval_checked": len(ret), "retrieval_hits": ret.count("hit"),
         "retrieval_errors": ret.count("error"),
         "retrieval_hit_rate": _rate(ret.count("hit"), len(ret))}
    if not retrieval_only:
        ans = [r["answer_check"]["status"] for r in results]
        s.update(answer_checked=len(ans), answer_passes=ans.count("pass"),
                 answer_errors=ans.count("error"), answer_pass_rate=_rate(ans.count("pass"), len(ans)))
    return s


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def format_report(report: dict, retrieval_only: bool) -> str:
    rows = []
    for i, r in enumerate(report["results"], 1):
        row = [str(i), r["question"][:48], r["retrieval"]["status"].upper(), r["retrieval"]["detail"][:34]]
        if not retrieval_only:
            row += [r["answer_check"]["status"].upper(), r["answer_check"]["detail"][:48]]
        rows.append(row)
    head = ["#", "Question", "Retrieval", "Detail"] + ([] if retrieval_only else ["Answer", "Detail"])
    widths = [max(len(x[c]) for x in [head, *rows]) for c in range(len(head))]

    def fmt(row):
        return "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip()

    lines = [fmt(head), fmt(["-" * w for w in widths]), *(fmt(r) for r in rows), ""]
    s = report["summary"]
    lines.append(f"Retrieval hit rate (top-{report['k']}): {s['retrieval_hits']}/{s['retrieval_checked']} "
                 f"= {_pct(s['retrieval_hit_rate'])}"
                 + (f"  ({s['retrieval_errors']} errors)" if s["retrieval_errors"] else ""))
    if not retrieval_only:
        lines.append(f"Answer pass rate: {s['answer_passes']}/{s['answer_checked']} = {_pct(s['answer_pass_rate'])}"
                     + (f"  ({s['answer_errors']} errors)" if s["answer_errors"] else ""))
    return "\n".join(lines)


def write_results(report: dict, data_dir: Path) -> Path:
    out_dir = Path(data_dir) / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"eval-{stamp}.json"
    payload = {"timestamp": datetime.now().isoformat(timespec="seconds"),
               "model": get_settings().openrouter_model, **report}
    path.write_text(json.dumps(payload, indent=2))
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--retrieval-only", action="store_true", help="skip the LLM answer checks")
    ap.add_argument("--limit", type=int, help="only run the first N questions")
    ap.add_argument("--k", type=int, default=DEFAULT_K, help=f"top-k for retrieval (default {DEFAULT_K})")
    ap.add_argument("--file", type=Path, default=DEFAULT_QUESTIONS, help="questions YAML")
    args = ap.parse_args(argv)

    questions = load_questions(args.file)
    if args.limit:
        questions = questions[:args.limit]
    s = get_settings()
    if not s.db_path.exists():
        print(f"No database at {s.db_path}; set DATA_DIR.", file=sys.stderr)
        return 2
    conn = db.connect()
    try:
        db.migrate(conn)
        report = run_eval(questions, retrieval_only=args.retrieval_only, k=args.k, conn=conn)
    finally:
        conn.close()
    print(format_report(report, args.retrieval_only))
    print(f"\nResults written to {write_results(report, s.data_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
