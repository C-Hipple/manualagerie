import sqlite3

EDITABLE_FIELDS = (
    "title", "device_name", "brand", "model_numbers", "category",
    "doc_type", "room", "summary", "notes",
)


def get_by_sha(conn: sqlite3.Connection, sha256: str):
    return conn.execute("SELECT * FROM manuals WHERE sha256 = ?", (sha256,)).fetchone()


def get(conn: sqlite3.Connection, manual_id: int):
    return conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()


def create(conn: sqlite3.Connection, sha256: str, filename: str, file_ext: str) -> int:
    cur = conn.execute(
        "INSERT INTO manuals (sha256, filename, file_ext, title) VALUES (?, ?, ?, ?)",
        (sha256, filename, file_ext, filename.rsplit(".", 1)[0]),
    )
    return cur.lastrowid


def list_all(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM manuals ORDER BY coalesce(device_name, title, filename) COLLATE NOCASE, id"
    ).fetchall()


def list_ready(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM manuals WHERE status = 'ready' "
        "ORDER BY coalesce(device_name, title, filename) COLLATE NOCASE, id"
    ).fetchall()


def update_fields(conn: sqlite3.Connection, manual_id: int, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS or k in (
        "status", "error", "page_count", "pages_done")}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE manuals SET {sets}, updated_at = datetime('now') WHERE id = ?",
        (*fields.values(), manual_id),
    )


def fill_metadata_if_empty(conn: sqlite3.Connection, manual_id: int, meta: dict, filename_title: str) -> None:
    """Apply LLM metadata, never overwriting values the user typed in."""
    row = get(conn, manual_id)
    updates = {}
    for k in EDITABLE_FIELDS:
        if k not in meta or meta[k] in (None, "", []):
            continue
        current = row[k]
        if current in (None, "") or (k == "title" and current == filename_title):
            updates[k] = meta[k]
    update_fields(conn, manual_id, updates)


def delete(conn: sqlite3.Connection, manual_id: int) -> None:
    conn.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))


def search_catalog(conn: sqlite3.Connection, query: str | None):
    rows = list_ready(conn)
    if not query:
        return rows
    terms = [t.lower() for t in query.split() if t.strip()]
    def score(r):
        hay = " ".join(str(r[k] or "") for k in (
            "title", "device_name", "brand", "model_numbers", "category", "doc_type", "room", "summary", "filename"
        )).lower()
        return sum(1 for t in terms if t in hay)
    scored = [(score(r), r) for r in rows]
    return [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]


# ---- pages & chunks ----

def replace_content(conn: sqlite3.Connection, manual_id: int, pages: list[dict], chunks: list[dict]) -> None:
    conn.execute("DELETE FROM chunks WHERE manual_id = ?", (manual_id,))
    conn.execute("DELETE FROM pages WHERE manual_id = ?", (manual_id,))
    conn.executemany(
        "INSERT INTO pages (manual_id, page_no, text, lang, ocr) VALUES (?, ?, ?, ?, ?)",
        [(manual_id, p["page_no"], p["text"], p.get("lang"), int(p.get("ocr", False))) for p in pages],
    )
    conn.executemany(
        "INSERT INTO chunks (manual_id, page_start, page_end, heading, lang, text) VALUES (?, ?, ?, ?, ?, ?)",
        [(manual_id, c["page_start"], c["page_end"], c.get("heading"), c.get("lang"), c["text"]) for c in chunks],
    )


def get_pages(conn: sqlite3.Connection, manual_id: int, start: int, end: int):
    return conn.execute(
        "SELECT page_no, text FROM pages WHERE manual_id = ? AND page_no BETWEEN ? AND ? ORDER BY page_no",
        (manual_id, start, end),
    ).fetchall()


def page_stats(conn: sqlite3.Connection, manual_id: int):
    return conn.execute(
        "SELECT count(*) AS n, sum(ocr) AS ocr, "
        "(SELECT count(*) FROM chunks WHERE manual_id = ?) AS chunks "
        "FROM pages WHERE manual_id = ?",
        (manual_id, manual_id),
    ).fetchone()
