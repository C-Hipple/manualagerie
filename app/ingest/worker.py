"""Background thread that processes ingest jobs and nightly backups."""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import db
from ..config import get_settings
from ..repo import jobs as jobs_repo
from ..repo import manuals as manuals_repo
from .chunk import chunk_pages
from .extract import extract_pages
from .lang import detect_lang
from .metadata import extract_metadata

log = logging.getLogger(__name__)

_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
BACKUP_INTERVAL_S = 24 * 3600


def wake() -> None:
    _wake.set()


def manual_path(manual) -> Path:
    return get_settings().files_dir / f"{manual['sha256']}.pdf"


def process_manual(conn, manual_id: int) -> None:
    s = get_settings()
    manual = manuals_repo.get(conn, manual_id)
    if manual is None:
        return
    path = manual_path(manual)
    manuals_repo.update_fields(conn, manual_id, {"status": "processing", "error": None, "pages_done": 0})
    conn.commit()

    last = [0.0]

    def progress(done: int, total: int):
        now = time.monotonic()
        if now - last[0] > 1 or done == total:
            last[0] = now
            manuals_repo.update_fields(conn, manual_id, {"pages_done": done, "page_count": total})
            conn.commit()

    pages = extract_pages(path, s.ocr_langs, progress)
    for p in pages:
        p.lang = detect_lang(p.text)
    chunks = chunk_pages(pages)
    manuals_repo.replace_content(
        conn, manual_id,
        [{"page_no": p.page_no, "text": p.text, "lang": p.lang, "ocr": p.ocr} for p in pages],
        chunks,
    )
    manuals_repo.update_fields(conn, manual_id, {"page_count": len(pages), "pages_done": len(pages)})
    conn.commit()

    if s.openrouter_api_key:
        try:
            opening = [p.text for p in pages[:5] if p.text.strip()][:4]
            meta = {}
            for attempt in range(2):  # models occasionally return an all-empty object
                meta = extract_metadata(manual["filename"], opening, len(pages))
                if meta.get("title"):
                    break
                log.info("metadata for manual %s came back empty (attempt %d)", manual_id, attempt + 1)
            log.info("metadata for manual %s: %s", manual_id, {k: meta.get(k) for k in ("title", "brand", "model_numbers")})
            manuals_repo.fill_metadata_if_empty(conn, manual_id, meta, manual["filename"].rsplit(".", 1)[0])
        except Exception as e:
            log.warning("metadata extraction failed for manual %s: %s", manual_id, e)

    total_text = sum(len(p.text.strip()) for p in pages)
    error = None if total_text else "No text could be extracted (scanned PDF and OCR unavailable?)"
    manuals_repo.update_fields(conn, manual_id, {"status": "ready", "error": error})
    conn.commit()
    log.info("manual %s ready: %d pages, %d chunks", manual_id, len(pages), len(chunks))


def run_backup(conn) -> Path:
    s = get_settings()
    dest = s.backups_dir / f"manualagerie-{datetime.now():%Y%m%d-%H%M%S}.db"
    conn.execute("VACUUM INTO ?", (str(dest),))
    backups = sorted(s.backups_dir.glob("manualagerie-*.db"))
    for old in backups[:-s.backup_keep] if s.backup_keep > 0 else []:
        old.unlink(missing_ok=True)
    log.info("backup written to %s", dest)
    return dest


def _last_backup_age() -> float:
    backups = sorted(get_settings().backups_dir.glob("manualagerie-*.db"))
    if not backups:
        return float("inf")
    return time.time() - backups[-1].stat().st_mtime


def _loop() -> None:
    conn = db.connect()
    try:
        n = jobs_repo.recover(conn)
        if n:
            log.info("re-queued %d interrupted jobs", n)
        while not _stop.is_set():
            if _last_backup_age() > BACKUP_INTERVAL_S:
                try:
                    run_backup(conn)
                except Exception:
                    log.exception("backup failed")
            job = jobs_repo.claim_next(conn)
            if job is None:
                _wake.wait(timeout=5)
                _wake.clear()
                continue
            try:
                process_manual(conn, job["manual_id"])
                jobs_repo.finish(conn, job["id"])
                conn.commit()
            except Exception as e:
                log.exception("job %s failed", job["id"])
                conn.rollback()
                retry = jobs_repo.fail(conn, job["id"], f"{type(e).__name__}: {e}")
                manuals_repo.update_fields(conn, job["manual_id"], {
                    "status": "queued" if retry else "error",
                    "error": None if retry else f"{type(e).__name__}: {e}",
                })
                conn.commit()
    finally:
        conn.close()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="ingest-worker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()
    if _thread:
        _thread.join(timeout=10)
