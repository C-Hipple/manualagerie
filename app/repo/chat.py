import json
import sqlite3


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


def get_message(conn: sqlite3.Connection, msg_id: int):
    return conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()


def question_before(conn: sqlite3.Connection, conv_id: int, msg_id: int) -> str | None:
    """The user message an assistant answer replied to."""
    row = conn.execute(
        "SELECT content FROM messages WHERE conversation_id = ? AND id < ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (conv_id, msg_id),
    ).fetchone()
    return row["content"] if row else None
