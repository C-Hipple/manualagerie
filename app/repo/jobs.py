import sqlite3

MAX_ATTEMPTS = 3


def enqueue(conn: sqlite3.Connection, manual_id: int, kind: str = "ingest") -> int:
    cur = conn.execute("INSERT INTO jobs (manual_id, kind) VALUES (?, ?)", (manual_id, kind))
    return cur.lastrowid


def claim_next(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = datetime('now') WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()


def finish(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'done', error = NULL, updated_at = datetime('now') WHERE id = ?", (job_id,))


def fail(conn: sqlite3.Connection, job_id: int, error: str) -> bool:
    """Returns True if the job will be retried."""
    row = conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    retry = row is not None and row["attempts"] < MAX_ATTEMPTS
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        ("queued" if retry else "error", error[:2000], job_id),
    )
    return retry


def recover(conn: sqlite3.Connection) -> int:
    cur = conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
    conn.execute("UPDATE manuals SET status = 'queued' WHERE status = 'processing'")
    conn.commit()
    return cur.rowcount
