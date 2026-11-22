import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from .. import chat_images, db
from ..config import get_settings
from ..llm import agent
from ..repo import chat as chat_repo
from ..repo import manuals as manuals_repo
from ..repo import notes as notes_repo
from ..web import templates

router = APIRouter()


def _script_json(obj) -> str:
    """JSON safe to inline in a <script> block, even when a title contains "</script>"."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(request: Request, conv_id: int | None):
    with db.get_conn() as conn:
        convs = chat_repo.list_conversations(conn)
        conv = chat_repo.get_conversation(conn, conv_id) if conv_id else None
        if conv_id and conv is None:
            raise HTTPException(404, "Conversation not found")
        msgs = [m for m in chat_repo.get_messages(conn, conv_id)
                if m["role"] == "user" or (m["role"] == "assistant" and not m["tool_calls"])] if conv else []
        images = chat_repo.images_by_message(conn, conv_id) if conv else {}
        manuals = manuals_repo.list_all(conn)
        ready = sum(1 for m in manuals if m["status"] == "ready")
        notes = notes_repo.list_all(conn)
    titles = {m["id"]: (m["title"] or m["filename"]) for m in manuals}
    note_titles = {n["id"]: n["title"] for n in notes}
    return templates.TemplateResponse(request, "chat.html", {
        "convs": convs, "conv": conv, "msgs": msgs, "titles_json": _script_json(titles), "ready_count": ready,
        "note_titles_json": _script_json(note_titles), "note_count": len(notes),
        "images": images, "chat_images": get_settings().chat_images, "max_images": chat_images.MAX_IMAGES,
    })


@router.get("/")
def chat_home(request: Request):
    return _page(request, None)


@router.get("/api/conversations/search")
def search_conversations(request: Request, q: str = ""):
    q = q.strip()[:200]
    with db.get_conn() as conn:
        if not q:
            return templates.TemplateResponse(request, "_conv_list.html", {
                "convs": chat_repo.list_conversations(conn), "conv": None, "hits": None, "q": ""})
        hits = chat_repo.search_conversations(conn, q)
    return templates.TemplateResponse(request, "_conv_list.html", {"convs": [], "conv": None, "hits": hits, "q": q})


@router.get("/c/{conv_id}")
def chat_conv(request: Request, conv_id: int):
    return _page(request, conv_id)


@router.post("/api/conversations")
def create_conversation():
    with db.get_conn() as conn:
        cid = chat_repo.create_conversation(conn)
    return {"id": cid}


@router.post("/c/{conv_id}/delete")
def delete_conversation(conv_id: int):
    with db.get_conn() as conn:
        chat_repo.delete_conversation(conn, conv_id)
    return RedirectResponse("/", status_code=303)


@router.get("/api/chat-images/{image_id}")
def chat_image(image_id: int):
    """An image attached to a chat question. Behind auth like every /api/ route (never on share pages)."""
    with db.get_conn() as conn:
        img = chat_repo.get_image(conn, image_id)
    path = chat_images.path_for(img["filename"]) if img else None
    if path is None or not path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(path, media_type=img["mime"], headers={
        "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


class MessageIn(BaseModel):
    text: str


async def _read_message(request: Request) -> tuple[str, list[bytes]]:
    """JSON {"text"} or multipart (text + up to MAX_IMAGES "images" files)."""
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        try:
            return MessageIn.model_validate_json(await request.body()).text, []
        except ValidationError:
            raise HTTPException(422, "Expected JSON {\"text\": ...}") from None
    form = await request.form(max_files=chat_images.MAX_IMAGES + 1, max_fields=10)
    text = form.get("text")
    files = [f for f in form.getlist("images") if not isinstance(f, str)]
    if len(files) > chat_images.MAX_IMAGES:
        raise HTTPException(400, f"Attach at most {chat_images.MAX_IMAGES} images.")
    blobs = []
    for f in files:
        data = await f.read(chat_images.MAX_UPLOAD_BYTES + 1)
        await f.close()
        blobs.append(data)
    return (text if isinstance(text, str) else ""), blobs


@router.post("/api/conversations/{conv_id}/messages")
async def send_message(request: Request, conv_id: int):
    raw_text, blobs = await _read_message(request)
    text = raw_text.strip()
    if blobs and not get_settings().chat_images:
        raise HTTPException(400, "Image attachments are turned off on this server (CHAT_IMAGES=false).")
    if not text and not blobs:
        raise HTTPException(400, "Empty message")
    images = []
    for i, data in enumerate(blobs, 1):
        try:
            images.append(await run_in_threadpool(chat_images.process, data))
        except chat_images.ImageRejected as e:
            raise HTTPException(400, f"Image {i}: {e}" if len(blobs) > 1 else str(e)) from None
    with db.get_conn() as conn:
        if chat_repo.get_conversation(conn, conv_id) is None:
            raise HTTPException(404, "Conversation not found")

    async def stream():
        async for ev in agent.run_turn(conv_id, text, str(request.base_url), images=images):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
