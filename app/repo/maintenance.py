import sqlite3
from datetime import date, timedelta

DUE_SOON_DAYS = 14
STATUS_LABELS = {"overdue": "Overdue", "soon": "Due soon", "ok": "OK"}


def _parse(d) -> date | None:
    try:
        return date.fromisoformat(str(d)[:10]) if d else None
    except ValueError:
        return None


def due_date(task) -> date | None:
    """last_done + interval; a task never done counts from the day it was created."""
    base = _parse(task["last_done"]) or _parse(task["created_at"])
    return base + timedelta(days=task["interval_days"]) if base else None


def status_of(task, today: date | None = None) -> str:
    today = today or date.today()
    due = due_date(task)
    if due is None:
        return "ok"
    if due < today:
        return "overdue"
    return "soon" if (due - today).days <= DUE_SOON_DAYS else "ok"


def decorate(task, today: date | None = None) -> dict:
    d = dict(task)
    d["due"] = due_date(task)
    d["status"] = status_of(task, today)
    d["status_label"] = STATUS_LABELS[d["status"]]
    return d


def list_all(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT t.*, coalesce(m.device_name, m.title, m.filename) AS manual_name, "
        "  (SELECT u.display_name FROM maintenance_log l LEFT JOIN users u ON u.id = l.created_by "
        "   WHERE l.task_id = t.id ORDER BY l.done_on DESC, l.id DESC LIMIT 1) AS last_done_by "
        "FROM maintenance_tasks t LEFT JOIN manuals m ON m.id = t.manual_id").fetchall()
    tasks = [decorate(t, today) for t in rows]
    tasks.sort(key=lambda t: (t["due"] or date.max, t["title"].lower()))
    return tasks


def list_for_manual(conn: sqlite3.Connection, manual_id: int, today: date | None = None) -> list[dict]:
    return [t for t in list_all(conn, today) if t["manual_id"] == manual_id]


def overdue_count(conn: sqlite3.Connection, today: date | None = None) -> int:
    return sum(1 for t in list_all(conn, today) if t["status"] == "overdue")


def get(conn: sqlite3.Connection, task_id: int):
    return conn.execute("SELECT * FROM maintenance_tasks WHERE id = ?", (task_id,)).fetchone()


def create(conn: sqlite3.Connection, title: str, interval_days: int, manual_id: int | None = None,
           last_done: str | None = None, notes: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO maintenance_tasks (title, manual_id, interval_days, last_done, notes) VALUES (?, ?, ?, ?, ?)",
        (title, manual_id, interval_days, last_done, notes))
    return cur.lastrowid


def update(conn: sqlite3.Connection, task_id: int, title: str, interval_days: int, manual_id: int | None,
           last_done: str | None, notes: str | None) -> None:
    conn.execute(
        "UPDATE maintenance_tasks SET title = ?, interval_days = ?, manual_id = ?, last_done = ?, notes = ? WHERE id = ?",
        (title, interval_days, manual_id, last_done, notes, task_id))


def delete(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute("DELETE FROM maintenance_tasks WHERE id = ?", (task_id,))


def mark_done(conn: sqlite3.Connection, task_id: int, done_on: date | None = None, note: str | None = None,
              created_by: int | None = None) -> date:
    """Log a completion (by `created_by`, a user id) and move last_done forward (never backwards)."""
    done_on = done_on or date.today()
    iso = done_on.isoformat()
    conn.execute("INSERT INTO maintenance_log (task_id, done_on, note, created_by) VALUES (?, ?, ?, ?)",
                 (task_id, iso, note, created_by))
    conn.execute("UPDATE maintenance_tasks SET last_done = ? WHERE id = ? AND (last_done IS NULL OR last_done < ?)",
                 (iso, task_id, iso))
    return done_on


def history(conn: sqlite3.Connection, task_id: int, limit: int = 20):
    return conn.execute("SELECT * FROM maintenance_log WHERE task_id = ? ORDER BY done_on DESC, id DESC LIMIT ?",
                        (task_id, limit)).fetchall()


def find(conn: sqlite3.Connection, ref) -> tuple[sqlite3.Row | None, list]:
    """Resolve a task id or title. Returns (task, candidates); task is None if missing or ambiguous."""
    rows = conn.execute("SELECT * FROM maintenance_tasks ORDER BY title COLLATE NOCASE").fetchall()
    ref_s = str(ref).strip()
    if ref_s.isdigit():
        for r in rows:
            if r["id"] == int(ref_s):
                return r, []
    low = ref_s.lower()
    exact = [r for r in rows if r["title"].lower() == low]
    if len(exact) == 1:
        return exact[0], []
    words = low.split()
    loose = [r for r in rows if low and (low in r["title"].lower() or r["title"].lower() in low)] or \
            [r for r in rows if words and all(w in r["title"].lower() for w in words)]
    if len(loose) == 1:
        return loose[0], []
    return None, (exact or loose)
