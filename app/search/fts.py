"""Keyword search over manual chunks using SQLite FTS5 / BM25."""
import re
import sqlite3

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "i",
    "in", "is", "it", "my", "of", "on", "or", "the", "this", "to", "what", "when", "where", "which",
    "with", "you", "your", "me", "should", "there", "that", "about", "we", "our",
}
NON_PREFERRED_LANG_PENALTY = 0.35  # multiply (negative) bm25 by this -> worse rank
_token_re = re.compile(r"[\w][\w\-./']*", re.UNICODE)


def build_match_query(query: str) -> str | None:
    tokens = []
    for raw in _token_re.findall(query):
        tok = raw.strip("-./'")
        if not tok or tok.lower() in STOPWORDS:
            continue
        tok = tok.replace('"', "")
        tokens.append(f'"{tok}"')
    if not tokens:
        return None
    seen = []
    for t in tokens:
        if t.lower() not in (x.lower() for x in seen):
            seen.append(t)
    return " OR ".join(seen)


def search(conn: sqlite3.Connection, query: str, manual_ids: list[int] | None = None, limit: int = 8,
           preferred_langs: list[str] | None = None) -> list[dict]:
    match = build_match_query(query)
    if not match:
        return []
    params: list = [match]
    where = "chunks_fts MATCH ? AND m.status = 'ready'"
    if manual_ids:
        where += f" AND c.manual_id IN ({','.join('?' * len(manual_ids))})"
        params.extend(int(x) for x in manual_ids)
    sql = f"""
        SELECT c.id AS chunk_id, c.manual_id, c.page_start, c.page_end, c.heading, c.lang, c.text,
               m.title AS manual_title, m.device_name, m.tags,
               bm25(chunks_fts, 3.0, 1.0) AS score,
               snippet(chunks_fts, 1, '«', '»', '…', 24) AS snippet
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN manuals m ON m.id = c.manual_id
        WHERE {where}
        ORDER BY score
        LIMIT ?
    """
    params.append(limit * 4)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    prefs = set(preferred_langs or [])
    if prefs:
        for r in rows:
            if r["lang"] and r["lang"] not in prefs:
                r["score"] *= NON_PREFERRED_LANG_PENALTY
        rows.sort(key=lambda r: r["score"])
    out, seen_text = [], set()
    for r in rows:
        key = r["text"][:200]
        if key in seen_text:
            continue
        seen_text.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out
