from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import db
from ..repo import chat as chat_repo
from ..repo import shares as shares_repo
from ..web import templates

router = APIRouter()

# The public page renders markdown in the browser from same-origin scripts only; it loads nothing else.
PUBLIC_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@router.get("/s/{token}")
def share_public(request: Request, token: str):
    """Public, logged-out view. Unknown and revoked tokens look the same."""
    with db.get_conn() as conn:
        share = shares_repo.get_public(conn, token)
    if share is None:
        raise HTTPException(404, "Not Found")
    return templates.TemplateResponse(request, "share.html", {
        "share": share, "citations": shares_repo.citations_of(share),
    }, headers=PUBLIC_HEADERS)


@router.get("/shares")
def shares_page(request: Request):
    with db.get_conn() as conn:
        shares = shares_repo.list_active(conn)
    base = str(request.base_url)
    return templates.TemplateResponse(request, "shares.html", {
        "shares": [(s, shares_repo.public_url(s["token"], base)) for s in shares],
    })


@router.post("/shares/{share_id}/revoke")
def share_revoke(request: Request, share_id: int):
    with db.get_conn() as conn:
        if shares_repo.get(conn, share_id) is None:
            raise HTTPException(404, "Share not found")
        shares_repo.revoke(conn, share_id)
    if request.headers.get("hx-request"):
        return Response(status_code=200)  # empty body: htmx swaps the row away
    return RedirectResponse("/shares", status_code=303)


@router.post("/api/messages/{msg_id}/share")
def share_message(request: Request, msg_id: int):
    """Snapshot one finished assistant answer as a share; returns the link fragment for htmx."""
    with db.get_conn() as conn:
        m = chat_repo.get_message(conn, msg_id)
        if m is None or m["role"] != "assistant" or m["tool_calls"] or not m["content"]:
            raise HTTPException(404, "Message not found")
        title = chat_repo.question_before(conn, m["conversation_id"], msg_id)
        _, token = shares_repo.create(conn, title, m["content"], conversation_id=m["conversation_id"])
    url = shares_repo.public_url(token, str(request.base_url))
    return templates.TemplateResponse(request, "_share_link.html", {"url": url})
