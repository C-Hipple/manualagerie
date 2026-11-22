"""Public share pages: read-only snapshots of an answer, reachable at /s/<token> without logging in."""
import json
import secrets
import sqlite3

from ..config import get_settings
from . import manuals as manuals_repo

TITLE_MAX = 120


def new_token() -> str:
    return secrets.token_urlsafe(16)


def public_url(token: str, base_url: str | None = None) -> str:
    """Absolute link from APP_URL, else from the request's base URL, else a relative path."""
    base = get_settings().app_url or base_url or ""
    return f"{base.rstrip('/')}/s/{token}"


def clean_title(title) -> str:
    title = " ".join(str(title or "").split()) or "Shared answer"
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rstrip() + "…"
    return title


def cite_text(conn: sqlite3.Connection, manual_id: int, page: int, page_end: int) -> str:
    m = manuals_repo.get(conn, manual_id)
    title = (m["title"] or m["filename"]) if m else "Manual"
    pages = f"p. {page}" if page == page_end else f"pp. {page}–{page_end}"
    return f"{title}, {pages}"


def snapshot(conn: sqlite3.Connection, body: str) -> tuple[str, list[str]]:
    """Turn chat citations into plain text, because the page is public and the manual PDFs are not.

    [#12 p.3] becomes "(Bosch dishwasher manual, p. 3)" and [note #4] becomes "(household note)":
    a note's title is the household's own wording, so it stays out unless the text itself says it.
    Returns the rewritten body and the distinct manual citations as text.
    """
    from ..llm.agent import CITATION_RE, NOTE_CITATION_RE  # agent imports the tools that import this module

    cites: list[str] = []

    def manual(m) -> str:
        a = int(m.group(2))
        text = cite_text(conn, int(m.group(1)), a, int(m.group(3)) if m.group(3) else a)
        if text not in cites:
            cites.append(text)
        return f"({text})"

    body = CITATION_RE.sub(manual, str(body or "").replace("\r\n", "\n")).strip()
    return NOTE_CITATION_RE.sub("(household note)", body), cites


def create(conn: sqlite3.Connection, title: str, body: str, *, conversation_id: int | None = None) -> tuple[int, str]:
    """Snapshot the body and store it under a fresh token. Returns (id, token)."""
    body, cites = snapshot(conn, body)
    token = new_token()
    share_id = conn.execute(
        "INSERT INTO shares (token, title, body, citations, conversation_id) VALUES (?, ?, ?, ?, ?)",
        (token, clean_title(title), body, json.dumps(cites) if cites else None, conversation_id),
    ).lastrowid
    return share_id, token


def get(conn: sqlite3.Connection, share_id: int):
    return conn.execute("SELECT * FROM shares WHERE id = ?", (share_id,)).fetchone()


def get_public(conn: sqlite3.Connection, token: str):
    """The live share for a token, or None when it is unknown or revoked."""
    return conn.execute("SELECT * FROM shares WHERE token = ? AND revoked_at IS NULL", (token,)).fetchone()


def list_active(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM shares WHERE revoked_at IS NULL ORDER BY created_at DESC, id DESC").fetchall()


def revoke(conn: sqlite3.Connection, share_id: int) -> None:
    conn.execute("UPDATE shares SET revoked_at = datetime('now') WHERE id = ? AND revoked_at IS NULL", (share_id,))


def citations_of(share) -> list[str]:
    return json.loads(share["citations"]) if share["citations"] else []
