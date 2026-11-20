import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import db
from ..config import get_settings

router = APIRouter()


@router.get("/healthz")
def healthz():
    s = get_settings()
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        writable = os.access(s.data_dir, os.W_OK)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    if not writable:
        return JSONResponse({"ok": False, "error": "data dir not writable"}, status_code=503)
    return {"ok": True}
