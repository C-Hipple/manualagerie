import asyncio
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import db
from app.llm import agent, tools
from app.main import app
from app.repo import chat as chat_repo
from app.repo import maintenance as repo
from app.repo import manuals as manuals_repo


def _login(c):
    assert c.post("/login", data={"password": "pw"}, follow_redirects=False).status_code == 303


def _clean(conn):
    conn.execute("DELETE FROM maintenance_tasks")
    conn.commit()


def _task_form(**kw):
    return {"title": "Change furnace filter", "interval_days": "90", "last_done": "", "manual_id": "",
            "notes": "", **kw}


def test_requires_login():
    with TestClient(app) as c:
        r = c.get("/maintenance", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.post("/maintenance", data=_task_form(), follow_redirects=False).status_code == 303
        assert c.post("/maintenance/1/done", headers={"hx-request": "true"}).status_code == 401


def test_crud_and_mark_done():
    with db.get_conn() as conn:
        _clean(conn)
    with TestClient(app) as c:
        _login(c)
        assert "No maintenance tasks" in c.get("/maintenance").text
        r = c.post("/maintenance", data=_task_form(title=" "), follow_redirects=False)
        assert "error=1" in r.headers["location"]
        r = c.post("/maintenance", data=_task_form(interval_days="0"), follow_redirects=False)
        assert "error=1" in r.headers["location"]
        r = c.post("/maintenance", data=_task_form(last_done=(date.today() - timedelta(days=100)).isoformat()),
                   follow_redirects=False)
        assert r.status_code == 303
        with db.get_conn() as conn:
            tid = repo.list_all(conn)[0]["id"]
        page = c.get("/maintenance").text
        assert "Change furnace filter" in page and "Overdue" in page and "1 overdue" in page

        c.post(f"/maintenance/{tid}", data=_task_form(title="Replace filter", interval_days="30"))
        assert "Replace filter" in c.get("/maintenance").text
        assert "Replace filter" in c.get(f"/maintenance?edit={tid}").text

        c.post(f"/maintenance/{tid}/done")
        with db.get_conn() as conn:
            t = repo.get(conn, tid)
            assert t["last_done"] == date.today().isoformat()
            assert [h["done_on"] for h in repo.history(conn, tid)] == [date.today().isoformat()]
        assert "1 overdue" not in c.get("/maintenance").text

        c.post(f"/maintenance/{tid}/delete")
        assert "No maintenance tasks" in c.get("/maintenance").text
        assert c.post("/maintenance/9999/done").status_code == 404


def test_due_computation_and_sorting(conn):
    _clean(conn)
    today = date(2026, 6, 1)
    a = repo.create(conn, "overdue", 30, last_done="2026-04-01")      # due 2026-05-01
    b = repo.create(conn, "soon", 30, last_done="2026-05-10")         # due 2026-06-09
    c_ = repo.create(conn, "fine", 365, last_done="2026-05-01")       # due 2027-05-01
    tasks = repo.list_all(conn, today)
    assert [t["title"] for t in tasks] == ["overdue", "soon", "fine"]
    assert [t["status"] for t in tasks] == ["overdue", "soon", "ok"]
    assert tasks[0]["due"] == date(2026, 5, 1)
    assert repo.overdue_count(conn, today) == 1
    # due today is not overdue; never-done tasks count from creation
    assert repo.status_of({"last_done": "2026-05-02", "created_at": "2026-01-01 00:00:00", "interval_days": 30},
                          date(2026, 6, 1)) == "soon"
    assert repo.status_of({"last_done": None, "created_at": "2026-01-01 00:00:00", "interval_days": 30},
                          date(2026, 6, 1)) == "overdue"
    # mark_done never moves last_done backwards but still logs
    repo.mark_done(conn, c_, date(2026, 1, 1))
    assert repo.get(conn, c_)["last_done"] == "2026-05-01"
    assert len(repo.history(conn, c_)) == 1
    _clean(conn)


def test_delete_manual_nulls_link(conn, tmp_path):
    _clean(conn)
    mid = manuals_repo.create(conn, "sha-maint-1", "Furnace.pdf", "pdf")
    tid = repo.create(conn, "Filter", 90, manual_id=mid)
    conn.commit()
    assert [t["id"] for t in repo.list_for_manual(conn, mid)] == [tid]
    with TestClient(app) as c:
        _login(c)
        assert "Filter" in c.get(f"/manuals/{mid}").text
        assert c.post(f"/manuals/{mid}/delete", follow_redirects=False).status_code == 303
    row = repo.get(conn, tid)
    assert row is not None and row["manual_id"] is None
    _clean(conn)


def test_log_maintenance_tool(conn):
    _clean(conn)
    tid = repo.create(conn, "Change furnace filter", 90, last_done="2026-01-01")
    conn.commit()
    out, _ = tools.dispatch(conn, "log_maintenance", json.dumps({"task": "furnace filter", "date": "2026-03-05"}))
    assert out.startswith("Logged") and "2026-03-05" in out
    assert repo.get(conn, tid)["last_done"] == "2026-03-05"
    out, _ = tools.dispatch(conn, "log_maintenance", json.dumps({"task": str(tid)}))
    assert repo.get(conn, tid)["last_done"] == date.today().isoformat()
    assert len(repo.history(conn, tid)) == 2
    for bad in ({"task": "garage door"}, {"task": tid, "date": "yesterday"},
                {"task": tid, "date": (date.today() + timedelta(days=3)).isoformat()}):
        out, _ = tools.dispatch(conn, "log_maintenance", json.dumps(bad))
        assert "Nothing was logged" in out
    assert len(repo.history(conn, tid)) == 2
    _clean(conn)


def test_agent_logs_via_tool_and_prompt_has_tasks(conn, monkeypatch):
    _clean(conn)
    tid = repo.create(conn, "Change furnace filter", 90, last_done="2026-01-01")
    conn.commit()
    monkeypatch.setattr(agent.get_settings(), "openrouter_api_key", "fake")
    calls = []

    async def fake_stream(messages, *, tools, tool_choice):
        calls.append(messages)
        if len(calls) == 1:
            yield "result", {"content": "", "usage": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "log_maintenance", "arguments": json.dumps({"task": "Change furnace filter"})}}]}
        else:
            assert messages[-1]["role"] == "tool" and messages[-1]["content"].startswith("Logged")
            text = "Logged: Change furnace filter done today."
            yield "token", text
            yield "result", {"content": text, "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()

    async def collect():
        return [ev async for ev in agent.run_turn(cid, "I just changed the furnace filter")]

    events = asyncio.run(collect())
    assert events[-1]["type"] == "done", events
    system = calls[0][0]["content"]
    assert "Change furnace filter" in system and "last done: 2026-01-01" in system
    assert "confirm in your reply" in system
    assert repo.get(conn, tid)["last_done"] == date.today().isoformat()
    _clean(conn)
