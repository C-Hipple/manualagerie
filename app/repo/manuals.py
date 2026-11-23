import sqlite3

from ..search import vector

EDITABLE_FIELDS = (
    "title", "device_name", "brand", "model_numbers", "category",
    "doc_type", "room", "tags", "summary", "notes",
)


def parse_tags(raw) -> list[str]:
    """Split a comma/semicolon-separated tag string into clean, unique tags."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = str(raw).replace(";", ",").split(",")
    out, seen = [], set()
    for part in parts:
        tag = " ".join(part.split())
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def normalize_tags(raw) -> str | None:
    tags = parse_tags(raw)
    return ", ".join(tags) if tags else None


def tags_of(manual) -> list[str]:
    return parse_tags(manual["tags"]) if manual is not None else []



def get_by_sha(conn: sqlite3.Connection, sha256: str):
    return conn.execute("SELECT * FROM manuals WHERE sha256 = ?", (sha256,)).fetchone()


def get(conn: sqlite3.Connection, manual_id: int):
    return conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()


def create(conn: sqlite3.Connection, sha256: str, filename: str, file_ext: str,
           source_url: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO manuals (sha256, filename, file_ext, title, source_url) VALUES (?, ?, ?, ?, ?)",
        (sha256, filename, file_ext, filename.rsplit(".", 1)[0], source_url),
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
    if "tags" in fields:
        fields["tags"] = normalize_tags(fields["tags"])
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
    vector.delete_manual(conn, manual_id)  # vec0 has no foreign keys, so no cascade
    conn.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))


def search_catalog(conn: sqlite3.Connection, query: str | None):
    rows = list_ready(conn)
    if not query:
        return rows
    terms = [t.lower() for t in query.split() if t.strip()]

    def hay(r, keys):
        return " ".join(str(r[k] or "") for k in keys).lower()

    def score(r):
        # The user's own tags and names outrank incidental words in a summary.
        tagged = hay(r, ("tags",))
        named = hay(r, ("device_name", "room", "title", "brand", "model_numbers", "category", "doc_type"))
        rest = hay(r, ("summary", "notes", "filename"))
        return sum(3 if t in tagged else 2 if t in named else 1 if t in rest else 0 for t in terms)

    scored = [(score(r), r) for r in rows]
    return [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]


# ---- pages & chunks ----

def replace_content(conn: sqlite3.Connection, manual_id: int, pages: list[dict], chunks: list[dict]) -> None:
    vector.delete_manual(conn, manual_id)  # chunk ids may be reused, so stale vectors must go first
    conn.execute("DELETE FROM chunks WHERE manual_id = ?", (manual_id,))
    conn.execute("DELETE FROM pages WHERE manual_id = ?", (manual_id,))
    conn.executemany(
        "INSERT INTO pages (manual_id, page_no, text, lang, ocr, ocr_method) VALUES (?, ?, ?, ?, ?, ?)",
        [(manual_id, p["page_no"], p["text"], p.get("lang"), int(p.get("ocr", False)),
          p.get("ocr_method") or ("tesseract" if p.get("ocr") else "text")) for p in pages],
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
        "SELECT count(*) AS n, sum(ocr_method = 'tesseract') AS ocr, sum(ocr_method = 'vision') AS vision, "
        "(SELECT count(*) FROM chunks WHERE manual_id = ?) AS chunks "
        "FROM pages WHERE manual_id = ?",
        (manual_id, manual_id),
    ).fetchone()
