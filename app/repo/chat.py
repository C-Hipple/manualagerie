import html
import json
import sqlite3

from ..config import get_settings
from ..search.fts import build_match_query

IMAGE_MARKER = "[image previously attached]"


def create_conversation(conn: sqlite3.Connection, title: str | None = None) -> int:
    return conn.execute("INSERT INTO conversations (title) VALUES (?)", (title,)).lastrowid


def list_conversations(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC, id DESC").fetchall()


def get_conversation(conn: sqlite3.Connection, conv_id: int):
    return conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()


def set_title(conn: sqlite3.Connection, conv_id: int, title: str) -> None:
    conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))


def touch(conn: sqlite3.Connection, conv_id: int) -> None:
    conn.execute("UPDATE conversations SET updated_at = datetime('now') WHERE id = ?", (conv_id,))


def delete_conversation(conn: sqlite3.Connection, conv_id: int) -> None:
    """Delete the conversation, its messages and the files of any images attached to it."""
    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM message_images WHERE conversation_id = ?", (conv_id,))]
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    d = get_settings().chat_images_dir
    for f in files:
        (d / f).unlink(missing_ok=True)


def content_text(content) -> str:
    """The text of a message's content, which may be a string or a list of OpenAI content parts."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def image_marker(n: int) -> str:
    return IMAGE_MARKER if n == 1 else f"[{n} images previously attached]"


def with_image_marker(text: str, n: int) -> str:
    """Earlier turns carry a short text marker instead of their images (see README "Asking with photos")."""
    if not n:
        return text
    return f"{text}\n{image_marker(n)}" if text else image_marker(n)


def images_by_message(conn: sqlite3.Connection, conv_id: int) -> dict[int, list]:
    out: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM message_images WHERE conversation_id = ? ORDER BY id", (conv_id,)):
        out.setdefault(r["message_id"], []).append(r)
    return out


def get_image(conn: sqlite3.Connection, image_id: int):
    return conn.execute("SELECT * FROM message_images WHERE id = ?", (image_id,)).fetchone()


def add_message(conn: sqlite3.Connection, conv_id: int, role: str, content: str | None = None, *,
                tool_calls=None, tool_call_id=None, citations=None, model=None, usage=None) -> int:
    return conn.execute(
        "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, citations, model, usage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conv_id, role, content_text(content) if isinstance(content, list) else content,
            json.dumps(tool_calls) if tool_calls else None,
            tool_call_id,
            json.dumps(citations) if citations else None,
            model,
            json.dumps(usage) if usage else None,
        ),
    ).lastrowid


def get_messages(conn: sqlite3.Connection, conv_id: int):
    return conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()


def to_llm_history(rows, image_counts: dict[int, int] | None = None) -> list[dict]:
    """Convert stored rows to OpenAI-format message dicts.

    Stored content is always text (attached images live in message_images), so content here is a string;
    a user turn that had images gets a text marker per image_counts ({message_id: n}) instead of the pixels.
    """
    image_counts = image_counts or {}
    out = []
    for r in rows:
        if r["role"] == "user" and image_counts.get(r["id"]):
            out.append({"role": "user", "content": with_image_marker(r["content"] or "", image_counts[r["id"]])})
            continue
        if r["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"] or ""})
        elif r["role"] == "assistant" and r["tool_calls"]:
            out.append({"role": "assistant", "content": r["content"] or "", "tool_calls": json.loads(r["tool_calls"])})
        else:
            out.append({"role": r["role"], "content": r["content"] or ""})
    return out


def get_message(conn: sqlite3.Connection, msg_id: int):
    return conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()


def question_before(conn: sqlite3.Connection, conv_id: int, msg_id: int) -> str | None:
    """The user message an assistant answer replied to."""
    row = conn.execute(
        "SELECT content FROM messages WHERE conversation_id = ? AND id < ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (conv_id, msg_id),
    ).fetchone()
    return row["content"] if row else None

_HL_OPEN, _HL_CLOSE = "\x02", "\x03"


def _render_snippet(raw: str) -> str:
    """HTML-escape FTS snippet text, then re-enable only the <mark> markers we inserted."""
    escaped = html.escape(raw.replace("\n", " "))
    return escaped.replace(_HL_OPEN, "<mark>").replace(_HL_CLOSE, "</mark>")


def search_conversations(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """Conversations with a message matching `query`, each with a safe HTML snippet (only <mark> tags)."""
    match = build_match_query(query)
    if not match:
        return []
    rows = conn.execute(
        """
        SELECT c.id, c.title,
               snippet(messages_fts, 0, char(2), char(3), '…', 16) AS snippet,
               bm25(messages_fts) AS score
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        JOIN conversations c ON c.id = m.conversation_id
        WHERE messages_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (match, limit * 5),
    ).fetchall()
    out, seen = [], set()
    for r in rows:  # best-ranked message per conversation
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({"id": r["id"], "title": r["title"], "snippet": _render_snippet(r["snippet"])})
        if len(out) >= limit:
            break
    return out
