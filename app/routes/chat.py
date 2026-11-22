import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from .. import db
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
        pending = bool(conv) and conv_id in agent.ACTIVE
        manuals = manuals_repo.list_all(conn)
        ready = sum(1 for m in manuals if m["status"] == "ready")
        notes = notes_repo.list_all(conn)
    titles = {m["id"]: (m["title"] or m["filename"]) for m in manuals}
    note_titles = {n["id"]: n["title"] for n in notes}
    return templates.TemplateResponse(request, "chat.html", {
        "convs": convs, "conv": conv, "msgs": msgs, "titles_json": _script_json(titles), "ready_count": ready,
        "note_titles_json": _script_json(note_titles), "note_count": len(notes), "pending": pending,
    })


@router.get("/")
def chat_home(request: Request):
    return _page(request, None)


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


class MessageIn(BaseModel):
    text: str


@router.post("/api/conversations/{conv_id}/messages")
async def send_message(conv_id: int, body: MessageIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Empty message")
    with db.get_conn() as conn:
        if chat_repo.get_conversation(conn, conv_id) is None:
            raise HTTPException(404, "Conversation not found")
    if conv_id in agent.ACTIVE:
        raise HTTPException(409, "Still answering the previous message")

    async def stream():
        async for ev in agent.start_turn(conv_id, text):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
