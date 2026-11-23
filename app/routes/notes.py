from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import auth, db
from ..repo import notes as notes_repo
from ..web import templates

router = APIRouter()


def _get_or_404(conn, note_id: int):
    n = notes_repo.get(conn, note_id)
    if n is None:
        raise HTTPException(404, "Note not found")
    return n


@router.get("/notes")
def notes_page(request: Request):
    with db.get_conn() as conn:
        notes = notes_repo.list_all(conn)
    return templates.TemplateResponse(request, "notes.html", {"notes": notes})


@router.post("/notes")
def note_create(request: Request, title: str = Form(""), body: str = Form(""), tags: str = Form("")):
    fields = notes_repo.clean(title, body, tags)
    if fields is None:
        return RedirectResponse("/notes?empty=1", status_code=303)
    with db.get_conn() as conn:
        note_id = notes_repo.create(conn, **fields, created_by=auth.current_user_id(request))
    return RedirectResponse(f"/notes#note-{note_id}", status_code=303)


@router.get("/notes/{note_id}")
def note_edit(request: Request, note_id: int):
    with db.get_conn() as conn:
        n = _get_or_404(conn, note_id)
    return templates.TemplateResponse(request, "note.html", {"n": n})


@router.post("/notes/{note_id}")
def note_update(note_id: int, title: str = Form(""), body: str = Form(""), tags: str = Form("")):
    fields = notes_repo.clean(title, body, tags)
    with db.get_conn() as conn:
        _get_or_404(conn, note_id)
        if fields is None:
            return RedirectResponse(f"/notes/{note_id}?empty=1", status_code=303)
        notes_repo.update(conn, note_id, **fields)
    return RedirectResponse(f"/notes#note-{note_id}", status_code=303)


@router.post("/notes/{note_id}/delete")
def note_delete(request: Request, note_id: int):
    with db.get_conn() as conn:
        _get_or_404(conn, note_id)
        notes_repo.delete(conn, note_id)
    if request.headers.get("hx-request"):
        return Response(status_code=200)  # empty body: htmx swaps the card away
    return RedirectResponse("/notes", status_code=303)
