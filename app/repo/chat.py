import html
import json
import sqlite3

from ..search.fts import build_match_query


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
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def add_message(conn: sqlite3.Connection, conv_id: int, role: str, content: str | None = None, *,
                tool_calls=None, tool_call_id=None, citations=None, model=None, usage=None) -> int:
    return conn.execute(
        "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, citations, model, usage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conv_id, role, content,
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


def to_llm_history(rows) -> list[dict]:
    """Convert stored rows to OpenAI-format message dicts."""
    out = []
    for r in rows:
        if r["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"] or ""})
        elif r["role"] == "assistant" and r["tool_calls"]:
            out.append({"role": "assistant", "content": r["content"] or "", "tool_calls": json.loads(r["tool_calls"])})
        else:
            out.append({"role": r["role"], "content": r["content"] or ""})
    return out


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
