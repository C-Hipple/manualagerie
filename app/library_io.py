"""Export the whole library to a zip, and replace it from one.

Layout of an export:

    manifest.json      format marker, schema version, export time, counts
    manualagerie.db    consistent snapshot made with VACUUM INTO (never the live file)
    files/...          the stored originals, exactly as they sit under DATA_DIR/files

The cookie secret, backups, tmp and any caches under DATA_DIR are never included.

Import validates the whole archive in a staging directory under DATA_DIR/tmp before touching
live data. The live DB is then replaced through SQLite's backup API, which rewrites it in one
write transaction: connections that other requests (or a streaming chat) hold stay valid and
see the new library, which renaming a file over a live WAL database would not guarantee. The
files directory is swapped with renames on the same filesystem.
"""
import json
import logging
import shutil
import sqlite3
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import db
from .config import get_settings
from .ingest import worker

log = logging.getLogger(__name__)

FORMAT = "manualagerie-export"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DB_NAME = "manualagerie.db"
FILES_PREFIX = "files/"
COUNTED_TABLES = ("manuals", "pages", "chunks", "conversations", "messages")
PRE_IMPORT_KEEP = 3
CHUNK = 1024 * 1024

# One export or import at a time; an import must never interleave with anything else that
# reads the files directory wholesale.
_lock = threading.Lock()


class InvalidArchive(Exception):
    """The archive was rejected. Nothing live has been changed."""


class Busy(Exception):
    pass


def schema_version() -> int:
    """The app's schema version: the number of the latest migration."""
    return max((int(f.name.split("_", 1)[0]) for f in db.MIGRATIONS_DIR.glob("*.sql")), default=0)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COUNTED_TABLES if t in have}


def _stored_files() -> list[Path]:
    files_dir = get_settings().files_dir
    if not files_dir.exists():
        return []
    return sorted(p for p in files_dir.rglob("*") if p.is_file() and not p.is_symlink())


# ---------------------------------------------------------------- export

def build_export() -> Path:
    """Write an export zip into DATA_DIR/tmp and return its path. The caller deletes it."""
    s = get_settings()
    if not _lock.acquire(timeout=30):
        raise Busy("an import or export is already running")
    token = uuid.uuid4().hex
    snap = s.tmp_dir / f"export-{token}.db"
    out = s.tmp_dir / f"export-{token}.zip"
    try:
        conn = db.connect()
        try:
            conn.execute("VACUUM INTO ?", (str(snap),))
        finally:
            conn.close()
        sconn = sqlite3.connect(snap)
        try:
            sconn.execute("DELETE FROM login_attempts")  # client IPs; not part of the library
            sconn.commit()
            version = sconn.execute("PRAGMA user_version").fetchone()[0]
            counts = _counts(sconn)
        finally:
            sconn.close()
        files = _stored_files()
        counts["files"] = len(files)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "schema_version": version,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": counts,
        }
        with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2))
            zf.write(snap, DB_NAME, compress_type=zipfile.ZIP_DEFLATED)
            for f in files:
                # PDFs are already compressed; storing them keeps export fast.
                zf.write(f, FILES_PREFIX + f.relative_to(s.files_dir).as_posix(), compress_type=zipfile.ZIP_STORED)
        return out
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        snap.unlink(missing_ok=True)
        _lock.release()


# ---------------------------------------------------------------- import

def _safe_member(name: str) -> PurePosixPath:
    """Reject zip-slip: absolute paths, drive letters, backslashes and any '..' component."""
    p = PurePosixPath(name)
    if (not name or name.startswith("/") or "\\" in name or "\x00" in name
            or (len(name) > 1 and name[1] == ":") or ".." in p.parts):
        raise InvalidArchive(f"unsafe path in archive: {name!r}")
    return p


def _check_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    s = get_settings()
    infos = zf.infolist()
    if len(infos) > s.import_max_files:
        raise InvalidArchive(f"archive has {len(infos)} entries; the limit is {s.import_max_files}")
    total, seen = 0, set()
    for info in infos:
        p = _safe_member(info.filename)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise InvalidArchive(f"symlink in archive: {info.filename!r}")
        if info.filename in seen:
            raise InvalidArchive(f"duplicate entry in archive: {info.filename!r}")
        seen.add(info.filename)
        in_files = p.parts[0] == "files" and (len(p.parts) > 1 or info.is_dir())
        if info.filename not in (MANIFEST, DB_NAME) and not in_files:
            raise InvalidArchive(f"unexpected entry in archive: {info.filename!r}")
        total += info.file_size
    if total > s.import_max_mb * 1024 * 1024:
        raise InvalidArchive(f"archive unpacks to more than {s.import_max_mb} MB")
    if MANIFEST not in seen:
        raise InvalidArchive("not a Manualagerie export: manifest.json is missing")
    if DB_NAME not in seen:
        raise InvalidArchive(f"not a Manualagerie export: {DB_NAME} is missing")
    return infos


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        with zf.open(MANIFEST) as f:
            m = json.loads(f.read(1024 * 1024))
    except Exception as e:
        raise InvalidArchive(f"manifest.json is unreadable: {e}") from None
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise InvalidArchive("manifest.json is not a Manualagerie export manifest")
    v = m.get("schema_version")
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise InvalidArchive("manifest.json has no valid schema_version")
    if v > schema_version():
        raise InvalidArchive(f"export is from a newer version of the app (schema {v}, this app has {schema_version()}); "
                           "upgrade the app first")
    return m


def _extract(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo], stage: Path) -> None:
    s = get_settings()
    budget = s.import_max_mb * 1024 * 1024
    root = stage.resolve()
    for info in infos:
        if info.filename == MANIFEST or info.is_dir():
            continue
        dest = (stage / info.filename).resolve()
        if not dest.is_relative_to(root):
            raise InvalidArchive(f"unsafe path in archive: {info.filename!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, dest.open("wb") as out:
            while data := src.read(CHUNK):
                budget -= len(data)
                if budget < 0:
                    raise InvalidArchive(f"archive unpacks to more than {s.import_max_mb} MB")
                out.write(data)


def _check_db(path: Path, manifest_version: int) -> None:
    """Integrity-check the staged DB and migrate it to the current schema, in staging."""
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as e:
        raise InvalidArchive(f"database could not be opened: {e}") from None
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise InvalidArchive(f"database failed its integrity check: {'; '.join(rows[:5])}")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != manifest_version:
            raise InvalidArchive(f"database schema {version} does not match the manifest ({manifest_version})")
        if version > schema_version():
            raise InvalidArchive(f"database is from a newer version of the app (schema {version})")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if version >= 1 and "manuals" not in tables:
            raise InvalidArchive("database has no manuals table")
        conn.execute("PRAGMA journal_mode = DELETE")
        live_page_size = _live_page_size()
        if conn.execute("PRAGMA page_size").fetchone()[0] != live_page_size:
            # The backup API cannot copy into a WAL database with a different page size.
            conn.execute(f"PRAGMA page_size = {int(live_page_size)}")
            conn.execute("VACUUM")
        try:
            db.migrate(conn)
        except sqlite3.Error as e:
            raise InvalidArchive(f"database could not be migrated to the current schema: {e}") from None
    except sqlite3.DatabaseError as e:
        raise InvalidArchive(f"database is unreadable: {e}") from None
    finally:
        conn.close()


def _live_page_size() -> int:
    conn = db.connect()
    try:
        return conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()


def _snapshot_current(dest: Path) -> None:
    """Pre-import backup: a VACUUM INTO copy of the live DB. The old files dir is moved in later."""
    dest.mkdir(parents=True)
    conn = db.connect()
    try:
        conn.execute("VACUUM INTO ?", (str(dest / DB_NAME),))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        conn.close()


def _prune_pre_import(backups_dir: Path) -> None:
    old = sorted(p for p in backups_dir.glob("pre-import-*") if p.is_dir())
    for p in old[:-PRE_IMPORT_KEEP]:
        shutil.rmtree(p, ignore_errors=True)


def _restore_db(staged: Path) -> None:
    src = sqlite3.connect(staged)
    live = db.connect()
    try:
        src.backup(live)
        live.execute("PRAGMA journal_mode = WAL")
        db.migrate(live)
        live.commit()
    finally:
        live.close()
        src.close()


def import_library(fileobj) -> dict:
    """Validate an export archive and replace the live library with it.

    Raises InvalidArchive (nothing changed) or Busy. Returns the manifest plus the backup location.
    """
    s = get_settings()
    if not _lock.acquire(blocking=False):
        raise Busy("an import or export is already running")
    try:
        for stale in s.tmp_dir.glob("import-*"):
            shutil.rmtree(stale, ignore_errors=True)
        stage = s.tmp_dir / f"import-{uuid.uuid4().hex}"
        stage.mkdir(parents=True)
        try:
            try:
                zf = zipfile.ZipFile(fileobj)
            except (zipfile.BadZipFile, OSError) as e:
                raise InvalidArchive(f"not a zip file: {e}") from None
            with zf:
                infos = _check_members(zf)
                manifest = _read_manifest(zf)
                try:
                    _extract(zf, infos, stage)
                except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as e:
                    raise InvalidArchive(f"archive is damaged: {e}") from None
            staged_db = stage / DB_NAME
            staged_files = stage / "files"
            staged_files.mkdir(exist_ok=True)
            _check_db(staged_db, manifest["schema_version"])

            # Validation done. From here on we touch live data.
            with worker.paused():
                backup = s.backups_dir / f"pre-import-{datetime.now():%Y%m%d-%H%M%S-%f}"
                _snapshot_current(backup)
                old_files = backup / "files"
                if s.files_dir.exists():
                    s.files_dir.rename(old_files)
                else:
                    old_files.mkdir()
                db_replaced = False
                try:
                    staged_files.rename(s.files_dir)
                    db_replaced = True  # from here a failure may have left the new DB in place
                    _restore_db(staged_db)
                except BaseException:
                    log.exception("import failed while swapping; putting the previous library back")
                    if s.files_dir.exists():
                        s.files_dir.rename(stage / "files-failed")
                    old_files.rename(s.files_dir)
                    if db_replaced:
                        _restore_db(backup / DB_NAME)
                    raise
            _prune_pre_import(s.backups_dir)
            log.info("library imported from export of %s; previous library kept at %s",
                     manifest.get("exported_at"), backup)
            return {**manifest, "backup": str(backup)}
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    finally:
        _lock.release()
