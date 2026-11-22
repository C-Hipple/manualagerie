import logging
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import db, library_io
from ..config import get_settings
from ..web import templates

log = logging.getLogger(__name__)
router = APIRouter()
CONFIRM_WORD = "REPLACE"


def _page(request: Request, status_code: int = 200, **ctx):
    with db.get_conn() as conn:
        manual_count = conn.execute("SELECT count(*) FROM manuals").fetchone()[0]
    return templates.TemplateResponse(request, "settings.html", {
        "manual_count": manual_count,
        "schema_version": library_io.schema_version(),
        "confirm_word": CONFIRM_WORD,
        "settings": get_settings(),
        **ctx,
    }, status_code=status_code)


@router.get("/settings")
def settings_page(request: Request):
    return _page(request)


@router.get("/settings/export")
def export_library(request: Request):
    try:
        path = library_io.build_export()
    except library_io.Busy as e:
        return _page(request, 409, error=str(e))
    name = f"manualagerie-export-{datetime.now():%Y%m%d-%H%M%S}.zip"
    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.post("/settings/import")
def import_library(request: Request, archive: UploadFile = File(...), confirm: str = Form("")):
    if confirm.strip() != CONFIRM_WORD:
        return _page(request, 400, error=f"Type {CONFIRM_WORD} to confirm that the import replaces the whole library.")
    try:
        result = library_io.import_library(archive.file)
    except library_io.InvalidArchive as e:
        return _page(request, 400, error=f"Import rejected, nothing was changed: {e}")
    except (library_io.Busy, TimeoutError) as e:
        return _page(request, 409, error=f"Import not started, nothing was changed: {e}")
    except Exception:
        log.exception("library import failed")
        return _page(request, 500, error="Import failed; the previous library was kept or put back. "
                                         "Check the server log for details.")
    counts = result.get("counts", {})
    return _page(request, imported=True,
                 message=f"Library replaced with the export from {result.get('exported_at', 'an unknown date')} "
                         f"({counts.get('manuals', '?')} manuals, {counts.get('files', '?')} files).",
                 backup=result["backup"])
