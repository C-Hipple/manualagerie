from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..repo import maintenance as repo
from ..repo import manuals as manuals_repo
from ..web import templates

router = APIRouter()


def _get_or_404(conn, task_id: int):
    t = repo.get(conn, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    return t


def _parse_form(form, conn) -> dict | None:
    title = str(form.get("title") or "").strip()
    try:
        interval = int(str(form.get("interval_days") or "").strip())
    except ValueError:
        return None
    if not title or interval < 1:
        return None
    last_done = str(form.get("last_done") or "").strip() or None
    if last_done:
        try:
            date.fromisoformat(last_done)
        except ValueError:
            return None
    manual_id = None
    raw = str(form.get("manual_id") or "").strip()
    if raw.isdigit() and manuals_repo.get(conn, int(raw)):
        manual_id = int(raw)
    return {"title": title, "interval_days": interval, "manual_id": manual_id, "last_done": last_done,
            "notes": str(form.get("notes") or "").strip() or None}


@router.get("/maintenance")
def maintenance_page(request: Request, edit: int | None = None):
    with db.get_conn() as conn:
        tasks = repo.list_all(conn)
        manuals = manuals_repo.list_all(conn)
        editing = repo.get(conn, edit) if edit else None
    return templates.TemplateResponse(request, "maintenance.html", {
        "tasks": tasks, "manuals": manuals, "editing": editing, "error": request.query_params.get("error")})


@router.post("/maintenance")
async def maintenance_create(request: Request):
    form = await request.form()
    with db.get_conn() as conn:
        data = _parse_form(form, conn)
        if data is None:
            return RedirectResponse("/maintenance?error=1", status_code=303)
        repo.create(conn, **data)
    return RedirectResponse("/maintenance", status_code=303)


@router.post("/maintenance/{task_id}")
async def maintenance_update(request: Request, task_id: int):
    form = await request.form()
    with db.get_conn() as conn:
        _get_or_404(conn, task_id)
        data = _parse_form(form, conn)
        if data is None:
            return RedirectResponse(f"/maintenance?edit={task_id}&error=1", status_code=303)
        repo.update(conn, task_id, **data)
    return RedirectResponse("/maintenance", status_code=303)


@router.post("/maintenance/{task_id}/done")
def maintenance_done(task_id: int):
    with db.get_conn() as conn:
        _get_or_404(conn, task_id)
        repo.mark_done(conn, task_id)
    return RedirectResponse("/maintenance", status_code=303)


@router.post("/maintenance/{task_id}/delete")
def maintenance_delete(task_id: int):
    with db.get_conn() as conn:
        _get_or_404(conn, task_id)
        repo.delete(conn, task_id)
    return RedirectResponse("/maintenance", status_code=303)
