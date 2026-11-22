import hashlib
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import db
from ..config import get_settings
from ..ingest import worker
from ..ingest.extract import IMAGE_EXTS, images_to_pdf, page_count
from ..repo import jobs as jobs_repo
from ..repo import manuals as manuals_repo
from ..search import hybrid, vector
from ..web import templates

router = APIRouter()
CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    pass


async def _save_upload(f: UploadFile, dest: Path, max_bytes: int) -> str:
    h = hashlib.sha256()
    size = 0
    with dest.open("wb") as out:
        while data := await f.read(CHUNK):
            size += len(data)
            if size > max_bytes:
                raise UploadTooLarge(f.filename)
            h.update(data)
            out.write(data)
    return h.hexdigest()


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _register(conn, sha: str, pdf_path: Path, filename: str, ext: str) -> tuple[int, bool]:
    """Move pdf into the library and queue ingest. Returns (manual_id, created)."""
    existing = manuals_repo.get_by_sha(conn, sha)
    if existing:
        pdf_path.unlink(missing_ok=True)
        return existing["id"], False
    final = get_settings().files_dir / f"{sha}.pdf"
    shutil.move(str(pdf_path), final)
    manual_id = manuals_repo.create(conn, sha, filename, ext)
    jobs_repo.enqueue(conn, manual_id, "ingest")
    return manual_id, True


@router.post("/manuals/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), combine_images: bool = Form(False)):
    s = get_settings()
    max_bytes = s.max_upload_mb * 1024 * 1024
    results: list[str] = []
    images: list[tuple[Path, str, str]] = []  # (tmp path, sha, filename)
    with db.get_conn() as conn:
        for f in files:
            if not f.filename:
                continue
            ext = _ext(f.filename)
            if ext != "pdf" and ext not in IMAGE_EXTS:
                results.append(f"⚠️ {f.filename}: unsupported file type (PDF or images only)")
                continue
            tmp = s.tmp_dir / f"{uuid.uuid4().hex}.{ext}"
            try:
                sha = await _save_upload(f, tmp, max_bytes)
            except UploadTooLarge:
                tmp.unlink(missing_ok=True)
                results.append(f"⚠️ {f.filename}: larger than {s.max_upload_mb} MB")
                continue
            if ext in IMAGE_EXTS:
                images.append((tmp, sha, f.filename))
                continue
            try:
                page_count(tmp)
            except Exception:
                tmp.unlink(missing_ok=True)
                results.append(f"⚠️ {f.filename}: not a readable PDF")
                continue
            mid, created = _register(conn, sha, tmp, f.filename, "pdf")
            results.append(f"{'✅ Added' if created else '↩️ Already in library'}: {f.filename}")
            conn.commit()

        groups = [images] if (combine_images and images) else [[i] for i in images]
        for group in groups:
            pdf_tmp = s.tmp_dir / f"{uuid.uuid4().hex}.pdf"
            try:
                images_to_pdf([p for p, _, _ in group], pdf_tmp)
            except Exception as e:
                results.append(f"⚠️ Could not convert {', '.join(n for _, _, n in group)}: {e}")
                continue
            finally:
                for p, _, _ in group:
                    p.unlink(missing_ok=True)
            sha = hashlib.sha256("".join(h for _, h, _ in group).encode()).hexdigest() if len(group) > 1 else group[0][1]
            name = group[0][2] if len(group) == 1 else f"{group[0][2].rsplit('.', 1)[0]} (+{len(group) - 1} photos)"
            mid, created = _register(conn, sha, pdf_tmp, name, _ext(group[0][2]))
            results.append(f"{'✅ Added' if created else '↩️ Already in library'}: {name}")
            conn.commit()
    worker.wake()
    if request.headers.get("hx-request"):
        return HTMLResponse("".join(f"<li>{templates.env.filters['e'](r)}</li>" for r in results))
    return RedirectResponse("/manuals", status_code=303)


@router.get("/manuals")
def library(request: Request):
    with db.get_conn() as conn:
        manuals = manuals_repo.list_all(conn)
    return templates.TemplateResponse(request, "manuals.html", {"manuals": manuals, "settings": get_settings()})


@router.get("/manuals/rows")
def library_rows(request: Request):
    with db.get_conn() as conn:
        manuals = manuals_repo.list_all(conn)
    return templates.TemplateResponse(request, "_manual_rows.html", {"manuals": manuals})


def _get_or_404(conn, manual_id: int):
    m = manuals_repo.get(conn, manual_id)
    if m is None:
        raise HTTPException(404, "Manual not found")
    return m


@router.get("/manuals/{manual_id}")
def manual_detail(request: Request, manual_id: int, page: int = 1):
    with db.get_conn() as conn:
        m = _get_or_404(conn, manual_id)
        stats = manuals_repo.page_stats(conn, manual_id)
    return templates.TemplateResponse(request, "manual.html", {"m": m, "stats": stats, "page": page})


@router.post("/manuals/{manual_id}")
async def manual_update(request: Request, manual_id: int):
    form = await request.form()
    fields = {k: (str(form.get(k)).strip() or None) for k in manuals_repo.EDITABLE_FIELDS if k in form}
    with db.get_conn() as conn:
        _get_or_404(conn, manual_id)
        manuals_repo.update_fields(conn, manual_id, fields)
    return RedirectResponse(f"/manuals/{manual_id}?saved=1", status_code=303)


@router.post("/manuals/{manual_id}/reindex")
def manual_reindex(manual_id: int):
    with db.get_conn() as conn:
        _get_or_404(conn, manual_id)
        manuals_repo.update_fields(conn, manual_id, {"status": "queued", "error": None})
        jobs_repo.enqueue(conn, manual_id, "reindex")
    worker.wake()
    return RedirectResponse(f"/manuals/{manual_id}", status_code=303)


@router.post("/manuals/{manual_id}/delete")
def manual_delete(request: Request, manual_id: int):
    with db.get_conn() as conn:
        m = _get_or_404(conn, manual_id)
        manuals_repo.delete(conn, manual_id)
    (get_settings().files_dir / f"{m['sha256']}.pdf").unlink(missing_ok=True)
    if request.headers.get("hx-request"):
        return Response(status_code=200)  # empty body: htmx swaps the row away
    return RedirectResponse("/manuals", status_code=303)


@router.get("/manuals/{manual_id}/file")
def manual_file(manual_id: int):
    with db.get_conn() as conn:
        m = _get_or_404(conn, manual_id)
    path = get_settings().files_dir / f"{m['sha256']}.pdf"
    if not path.exists():
        raise HTTPException(404, "File missing from storage")
    name = m["filename"] if m["filename"].lower().endswith(".pdf") else m["filename"].rsplit(".", 1)[0] + ".pdf"
    return FileResponse(path, media_type="application/pdf", filename=name, content_disposition_type="inline")


@router.get("/search")
def search_page(request: Request, q: str = "", manual_id: int | None = None):
    hits = []
    with db.get_conn() as conn:
        manuals = manuals_repo.list_ready(conn)
        if q:
            hits = hybrid.search(conn, q, [manual_id] if manual_id else None, 20, get_settings().preferred_lang_list)
    return templates.TemplateResponse(request, "search.html",
                                      {"q": q, "hits": hits, "manuals": manuals, "manual_id": manual_id,
                                       "semantic_status": vector.status()})
