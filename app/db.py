import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_settings().db_path
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version <= current:
            continue
        conn.executescript(f.read_text())
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current = version
    return current


def init_storage() -> None:
    s = get_settings()
    for d in (s.data_dir, s.files_dir, s.tmp_dir, s.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    for f in s.tmp_dir.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()
