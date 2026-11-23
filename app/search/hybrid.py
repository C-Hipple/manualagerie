"""The search behind `search_manuals` and /search: FTS5 + vectors merged with Reciprocal Rank Fusion.

Falls back to FTS-only (same rows `fts.search` returns) whenever semantic search is off or unavailable.
"""
import logging
import sqlite3

from . import fts, vector

log = logging.getLogger(__name__)

CANDIDATES = 30  # top-N taken from each source before fusing
RRF_K = 60
SNIPPET_WORDS = 40


def rrf_merge(ranked_lists: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float, dict[str, int]]]:
    """Fuse ranked id lists: score(id) = sum over sources of 1 / (k + rank), rank starting at 1.

    Returns (id, score, {source: rank}) best first; ties go to the id with the better single
    rank, then to the earlier source list, so the order is deterministic.
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for source, ids in ranked_lists.items():
        for rank, item in enumerate(ids, start=1):
            if source in ranks.get(item, {}):
                continue  # count an id once per source
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(item, {})[source] = rank
    order = list(ranked_lists)
    return sorted(
        ((i, scores[i], ranks[i]) for i in scores),
        key=lambda t: (-t[1], min(t[2].values()), min(order.index(s) for s in t[2])),
    )


def _plain_snippet(text: str) -> str:
    words = text.split()
    return " ".join(words[:SNIPPET_WORDS]) + ("…" if len(words) > SNIPPET_WORDS else "")


def search(conn: sqlite3.Connection, query: str, manual_ids: list[int] | None = None, limit: int = 8,
           preferred_langs: list[str] | None = None) -> list[dict]:
    """Rows shaped like `fts.search` plus `sources` (["fts"], ["vector"] or both) and `rrf`.

    `score` stays "lower is better" in both modes: BM25 when FTS-only, -RRF when fused.
    """
    vec_rows = vector.search(conn, query, manual_ids, CANDIDATES, preferred_langs)
    if vec_rows is None:  # semantic search off/unavailable: plain keyword search
        rows = fts.search(conn, query, manual_ids, limit, preferred_langs)
        for r in rows:
            r["sources"] = ["fts"]
        return rows

    fts_rows = fts.search(conn, query, manual_ids, CANDIDATES, preferred_langs)
    by_id: dict[int, dict] = {}
    for r in vec_rows:
        by_id[r["chunk_id"]] = r | {"snippet": _plain_snippet(r["text"]), "score": None}
    for r in fts_rows:  # FTS rows win: they carry the highlighted snippet and BM25
        by_id[r["chunk_id"]] = r | {"distance": by_id.get(r["chunk_id"], {}).get("distance")}
    fused = rrf_merge({"fts": [r["chunk_id"] for r in fts_rows], "vector": [r["chunk_id"] for r in vec_rows]})

    out, seen_text = [], set()
    for chunk_id, score, ranks in fused:
        row = by_id[chunk_id]
        key = row["text"][:200]
        if key in seen_text:
            continue
        seen_text.add(key)
        out.append(row | {
            "bm25": row.get("score") if "fts" in ranks else None,
            "score": -score,
            "rrf": score,
            "sources": [s for s in ("fts", "vector") if s in ranks],
            "ranks": ranks,
        })
        if len(out) >= limit:
            break
    return out
