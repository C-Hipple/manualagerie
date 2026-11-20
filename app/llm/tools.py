"""Tool schemas exposed to the model and their local implementations."""
import json
import sqlite3

from ..config import get_settings
from ..repo import manuals as manuals_repo
from ..search import fts
from .prompts import catalog_line

MAX_READ_PAGES = 4

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_manuals",
            "description": "Keyword search (BM25) over the text of the user's manuals. Returns the best matching "
                           "passages with manual id and page numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to search for, e.g. 'drain hose diameter connection'"},
                    "manual_ids": {"type": "array", "items": {"type": "integer"},
                                   "description": "Restrict to these manual ids (from the catalog). Omit to search everything."},
                    "limit": {"type": "integer", "description": "Max passages (default 6, max 12)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pages",
            "description": f"Read the full extracted text of up to {MAX_READ_PAGES} consecutive pages of one manual. "
                           "Use for spec tables, part lists and procedures that search snippets cut off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "manual_id": {"type": "integer"},
                    "start_page": {"type": "integer"},
                    "end_page": {"type": "integer"},
                },
                "required": ["manual_id", "start_page"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_manuals",
            "description": "List manuals in the library, optionally filtered by words matching the user's tags, "
                           "device name, room, notes, brand, model number, category or title. Use it to find "
                           "which manual a nickname or location like \"downstairs AC\" refers to.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    },
]


def _cite(manual_id: int, a: int, b: int) -> str:
    return f"[#{manual_id} p.{a}]" if a == b else f"[#{manual_id} p.{a}-{b}]"


def search_manuals(conn: sqlite3.Connection, query: str, manual_ids=None, limit: int | None = None) -> str:
    limit = max(1, min(int(limit or 6), 12))
    ids = [int(x) for x in manual_ids] if manual_ids else None
    hits = fts.search(conn, query, ids, limit, get_settings().preferred_lang_list)
    if not hits:
        return f"No passages matched {query!r}" + (f" in manuals {ids}" if ids else "") + \
            ". Try different keywords or synonyms, or search all manuals."
    parts = []
    for h in hits:
        head = f"{_cite(h['manual_id'], h['page_start'], h['page_end'])} {h['manual_title'] or ''}"
        if h.get("device_name"):
            head += f" ({h['device_name']})"
        if h.get("tags"):
            head += f" [tags: {h['tags']}]"
        if h["heading"]:
            head += f" — section: {h['heading']}"
        parts.append(f"{head}\n{h['text']}")
    return "\n\n---\n\n".join(parts)


def read_pages(conn: sqlite3.Connection, manual_id: int, start_page: int, end_page: int | None = None) -> str:
    manual = manuals_repo.get(conn, int(manual_id))
    if manual is None:
        return f"No manual with id {manual_id}."
    start = max(1, int(start_page))
    end = int(end_page or start)
    end = max(start, min(end, start + MAX_READ_PAGES - 1))
    rows = manuals_repo.get_pages(conn, manual["id"], start, end)
    if not rows:
        return f"Manual #{manual_id} has {manual['page_count']} pages; pages {start}-{end} not found."
    out = [f"Manual #{manual['id']}: {manual['title']} ({manual['page_count']} pages)"]
    for r in rows:
        out.append(f"{_cite(manual['id'], r['page_no'], r['page_no'])}\n{r['text'] or '(no text on this page — likely a diagram)'}")
    return "\n\n".join(out)


def list_manuals(conn: sqlite3.Connection, query: str | None = None) -> str:
    rows = manuals_repo.search_catalog(conn, query)
    if not rows:
        return "No manuals matched." if query else "The library is empty."
    return "\n".join(catalog_line(m) for m in rows[:100])


def status_text(name: str, args: dict, conn: sqlite3.Connection) -> str:
    if name == "search_manuals":
        where = ""
        if args.get("manual_ids"):
            titles = []
            for i in args["manual_ids"][:3]:
                m = manuals_repo.get(conn, int(i))
                if m:
                    titles.append(m["title"] or m["filename"])
            if titles:
                where = " in " + ", ".join(titles)
        return f"Searching{where} for “{args.get('query', '')}”"
    if name == "read_pages":
        m = manuals_repo.get(conn, int(args.get("manual_id", 0)))
        title = (m["title"] or m["filename"]) if m else f"manual #{args.get('manual_id')}"
        s, e = args.get("start_page"), args.get("end_page") or args.get("start_page")
        return f"Reading {title}, page{'s' if s != e else ''} {s}{'–' + str(e) if s != e else ''}"
    if name == "list_manuals":
        return "Looking through the manual library"
    return name


def dispatch(conn: sqlite3.Connection, name: str, raw_args: str) -> tuple[str, dict]:
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
    except (json.JSONDecodeError, ValueError) as e:
        return f"Invalid arguments: {e}", {}
    try:
        if name == "search_manuals":
            return search_manuals(conn, args.get("query", ""), args.get("manual_ids"), args.get("limit")), args
        if name == "read_pages":
            return read_pages(conn, args["manual_id"], args["start_page"], args.get("end_page")), args
        if name == "list_manuals":
            return list_manuals(conn, args.get("query")), args
    except (KeyError, TypeError, ValueError) as e:
        return f"Tool error: {type(e).__name__}: {e}", args
    return f"Unknown tool {name}", args
