import asyncio
import json

from app.llm import agent, tools
from app.llm.prompts import catalog_line
from app.repo import chat as chat_repo
from app.repo import manuals as manuals_repo
from tests.test_ingest import _ingest


def test_parse_citations():
    text = "Use 3/8\" [#12 p.21] and see [#12 p.21] [#3 p.4-5] [#7 pp. 9–10]"
    assert agent.parse_citations(text) == [
        {"manual_id": 12, "page": 21, "page_end": 21},
        {"manual_id": 3, "page": 4, "page_end": 5},
        {"manual_id": 7, "page": 9, "page_end": 10},
    ]


def test_tools_dispatch(conn, pdf_path):
    mid = _ingest(conn, pdf_path)
    out, _ = tools.dispatch(conn, "search_manuals", json.dumps({"query": "drain hose", "manual_ids": [mid]}))
    assert f"[#{mid} p.3]" in out
    out, _ = tools.dispatch(conn, "read_pages", json.dumps({"manual_id": mid, "start_page": 2, "end_page": 99}))
    assert "3/8 inch" in out and f"[#{mid} p.5]" in out and f"[#{mid} p.6]" not in out
    out, _ = tools.dispatch(conn, "read_pages", "not json")
    assert out.startswith("Invalid arguments")
    out, _ = tools.dispatch(conn, "list_manuals", json.dumps({"query": "dishwasher"}))
    assert f"#{mid}" in out


def test_user_tags_identify_the_right_manual(conn, pdf_path):
    mid = _ingest(conn, pdf_path)
    manuals_repo.update_fields(conn, mid, {
        "device_name": "Air conditioner",
        "tags": "downstairs AC, basement unit",
        "notes": "The upstairs unit is a different manual.",
    })
    conn.commit()

    line = catalog_line(manuals_repo.get(conn, mid))
    assert f"#{mid}" in line and "downstairs AC" in line

    out, _ = tools.dispatch(conn, "list_manuals", json.dumps({"query": "downstairs ac"}))
    assert f"#{mid}" in out
    out, _ = tools.dispatch(conn, "list_manuals", json.dumps({"query": "basement"}))
    assert f"#{mid}" in out


def test_tool_loop(conn, pdf_path, monkeypatch):
    mid = _ingest(conn, pdf_path)
    monkeypatch.setattr(agent.get_settings(), "openrouter_api_key", "fake")
    calls = []

    async def fake_stream(messages, *, tools, tool_choice):
        calls.append(messages)
        if len(calls) == 1:
            yield "result", {"content": "", "usage": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "search_manuals", "arguments": json.dumps({"query": "drain hose size"})}}]}
        else:
            assert messages[-1]["role"] == "tool" and "5/8 inch" in messages[-1]["content"]
            text = f"The drain hose fits 5/8\" to 1\" connections [#{mid} p.3]."
            yield "token", text
            yield "result", {"content": text, "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()

    async def collect():
        return [ev async for ev in agent.run_turn(cid, "What size is the dishwasher drain hose?")]

    events = asyncio.run(collect())
    types = [e["type"] for e in events]
    assert types[0] == "status" and "token" in types and types[-1] == "done", events
    assert events[-1]["citations"][0]["manual_id"] == mid
    assert "Bosch" in calls[0][0]["content"] or "dishwasher" in calls[0][0]["content"]
    rows = chat_repo.get_messages(conn, cid)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert chat_repo.get_conversation(conn, cid)["title"].startswith("What size")
