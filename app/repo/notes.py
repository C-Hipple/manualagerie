"""Household notes: short free-form facts that aren't tied to any manual."""
import sqlite3

from .manuals import normalize_tags

TITLE_MAX = 120


def clean(title, body, tags) -> dict | None:
    """Normalize form input. Returns None when there is nothing to save.

    A note needs a title, but people often just type the fact; the first line
    of the body then becomes the title (and leaves the body, unless it is too
    long to be a title, so the same words aren't shown twice).
    """
    title = " ".join(str(title or "").split())
    body = str(body or "").replace("\r\n", "\n").strip()
    if not title and body:
        first, _, rest = body.partition("\n")
        title = " ".join(first.split())
        if len(title) <= TITLE_MAX:
            body = rest.strip()
    if not title:
        return None
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rstrip() + "…"
    return {"title": title, "body": body or None, "tags": normalize_tags(tags)}


def get(conn: sqlite3.Connection, note_id: int):
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def list_all(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM notes ORDER BY title COLLATE NOCASE, id").fetchall()


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM notes").fetchone()[0]


def create(conn: sqlite3.Connection, title: str, body: str | None = None, tags: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO notes (title, body, tags) VALUES (?, ?, ?)", (title, body, tags),
    ).lastrowid


def update(conn: sqlite3.Connection, note_id: int, title: str, body: str | None, tags: str | None) -> None:
    conn.execute(
        "UPDATE notes SET title = ?, body = ?, tags = ?, updated_at = datetime('now') WHERE id = ?",
        (title, body, tags, note_id),
    )


def delete(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def search(conn: sqlite3.Connection, query: str | None):
    """Notes ranked by how well their tags, title and body match the query words."""
    rows = list_all(conn)
    terms = [t.lower() for t in (query or "").split() if t.strip()]
    if not terms:
        return rows

    def score(r):
        tags, title, body = ((r[k] or "").lower() for k in ("tags", "title", "body"))
        return sum(3 if t in tags else 2 if t in title else 1 if t in body else 0 for t in terms)

    scored = [(score(r), r) for r in rows]
    return [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]
