import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

try:
    import sqlite_vec
except ImportError:  # optional: semantic search degrades to FTS-only
    sqlite_vec = None

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# A migration whose first lines contain this marker needs the sqlite-vec extension.
REQUIRES_VEC = "-- requires: sqlite-vec"
_vec_warned = False


def load_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into this connection. Never raises; warns once if it can't."""
    global _vec_warned
    if sqlite_vec is None:
        reason = "the sqlite-vec package is not installed"
    else:
        try:
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
            return True
        except (AttributeError, sqlite3.Error, OSError) as e:  # AttributeError: Python built without extensions
            reason = f"{type(e).__name__}: {e}"
    if not _vec_warned:
        _vec_warned = True
        log.warning("sqlite-vec unavailable (%s): semantic search falls back to keyword-only", reason)
    return False


def has_vec(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except sqlite3.Error:
        return False


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_settings().db_path
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    load_vec(conn)  # before anything touches vec0 tables, migrations included
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


def _needs_vec(sql: str) -> bool:
    return REQUIRES_VEC in "\n".join(sql.splitlines()[:5])


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    vec = has_vec(conn)
    for f in files:
        version = int(f.name.split("_", 1)[0])
        sql = f.read_text()
        if version <= current:
            # Vec migrations are idempotent (IF NOT EXISTS) and are re-run whenever the extension is
            # present, so a DB migrated while sqlite-vec was missing gains its tables later.
            if vec and _needs_vec(sql):
                conn.executescript(sql)
                conn.commit()
            continue
        if _needs_vec(sql) and not vec:
            log.warning("migration %s skipped for now: it needs sqlite-vec", f.name)
        else:
            conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current = version
    return current


def init_storage() -> None:
    s = get_settings()
    for d in (s.data_dir, s.files_dir, s.tmp_dir, s.backups_dir, s.models_dir):
        d.mkdir(parents=True, exist_ok=True)
    for f in s.tmp_dir.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()
