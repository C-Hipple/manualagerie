import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.llm import agent, tools
from app.main import app
from app.repo import chat as chat_repo
from app.repo import manuals as manuals_repo
from app.repo import shares as shares_repo
from tests.conftest import login
from tests.test_ingest import _ingest

TOKEN_RE = re.compile(r"/s/([A-Za-z0-9_-]+)")


@pytest.fixture(autouse=True)
def _clean_shares(conn):
    """The test database is shared across modules; leave no shares behind."""
    yield
    conn.execute("DELETE FROM shares")
    conn.commit()


def _login(c):
    login(c)


def _share(conn, title="Drain hose", body="Fits **5/8 inch**."):
    _, token = shares_repo.create(conn, title, body)
    conn.commit()
    return token


def test_migration_adds_shares_to_an_existing_library(tmp_path):
    from app import db
    old = db.connect(tmp_path / "old.db")
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql"))[:3]:
        old.executescript(f.read_text())
    old.execute("PRAGMA user_version = 3")
    old.commit()
    assert db.migrate(old) >= 4
    old.execute("INSERT INTO shares (token, title, body) VALUES ('t', 'x', 'y')")
    assert old.execute("SELECT count(*) FROM shares").fetchone()[0] == 1
    old.close()


def test_public_url(monkeypatch):
    s = agent.get_settings()
    monkeypatch.setattr(s, "app_url", "https://manuals.example.com/")
    assert shares_repo.public_url("abc", "http://testserver/") == "https://manuals.example.com/s/abc"
    monkeypatch.setattr(s, "app_url", "")
    assert shares_repo.public_url("abc", "http://testserver/") == "http://testserver/s/abc"
    assert shares_repo.public_url("abc") == "/s/abc"
    assert len(shares_repo.new_token()) >= 22


def test_create_share_tool(conn, pdf_path, monkeypatch):
    mid = _ingest(conn, pdf_path)
    title = manuals_repo.get(conn, mid)["title"] or manuals_repo.get(conn, mid)["filename"]
    assert "create_share" in [t["function"]["name"] for t in tools.TOOLS]
    monkeypatch.setattr(agent.get_settings(), "app_url", "https://manuals.example.com")
    out, _ = tools.dispatch(conn, "create_share", json.dumps(
        {"title": "Dishwasher drain hose", "content": f"Fits 5/8\" to 1\" [#{mid} p.3]. See [note #9]."}))
    conn.commit()
    m = re.search(r"https://manuals\.example\.com/s/([A-Za-z0-9_-]{22,})", out)
    assert m, out
    share = shares_repo.get_public(conn, m.group(1))
    assert share["title"] == "Dishwasher drain hose"
    # Citations are snapshotted as plain text: the PDFs and notes are private.
    assert share["body"] == f"Fits 5/8\" to 1\" ({title}, p. 3). See (household note)."
    assert shares_repo.citations_of(share) == [f"{title}, p. 3"]
    out, _ = tools.dispatch(conn, "create_share", json.dumps({"title": "x", "content": "  "}))
    assert out.startswith("Nothing to share")
    out, _ = tools.dispatch(conn, "create_share", json.dumps({"title": "x"}))
    assert out.startswith("Tool error")


def test_tool_loop_creates_share(conn, monkeypatch):
    monkeypatch.setattr(agent.get_settings(), "openrouter_api_key", "fake")
    calls = []

    async def fake_stream(messages, *, tools, tool_choice):
        calls.append(messages)
        if len(calls) == 1:
            yield "result", {"content": "", "usage": None, "tool_calls": [{
                "id": "c1", "type": "function", "function": {"name": "create_share", "arguments": json.dumps(
                    {"title": "Filter size", "content": "The fridge takes filter **XWF**."})}}]}
        else:
            url = re.search(r"\S+/s/[A-Za-z0-9_-]+", messages[-1]["content"]).group(0)
            text = f"Here you go: [Filter size]({url})"
            yield "token", text
            yield "result", {"content": text, "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()

    async def collect():
        return [ev async for ev in agent.run_turn(cid, "Make me a link for the fridge filter", "http://testserver/")]

    events = asyncio.run(collect())
    assert events[0] == {"type": "status", "text": "Creating a share link for “Filter size”"}
    assert events[-1]["type"] == "done"
    assert "create_share" in calls[0][0]["content"]  # the system prompt explains when to use it
    answer = chat_repo.get_messages(conn, cid)[-1]["content"]
    token = re.search(r"\(http://testserver/s/([A-Za-z0-9_-]+)\)", answer).group(1)
    share = shares_repo.get_public(conn, token)
    assert share["conversation_id"] == cid and "XWF" in share["body"]

    with TestClient(app) as c:  # logged out
        r = c.get(f"/s/{token}", follow_redirects=False)
        assert r.status_code == 200 and "Filter size" in r.text and "filter **XWF**" in r.text


def test_public_page_logged_out(conn):
    token = _share(conn)
    with TestClient(app) as c:
        r = c.get(f"/s/{token}", follow_redirects=False)
        assert r.status_code == 200
        page = r.text
        assert '<meta name="robots" content="noindex">' in page and r.headers["x-robots-tag"] == "noindex"
        assert "/static/themes.css" in page and "Drain hose" in page
        assert "topbar" not in page and "/logout" not in page and 'href="/manuals' not in page
        assert "script-src 'self'" in r.headers["content-security-policy"]


def test_unknown_and_revoked_tokens_404(conn):
    token = _share(conn)
    with TestClient(app) as c:
        assert c.get("/s/not-a-real-token").status_code == 404
        _login(c)
        page = c.get("/shares").text
        assert f"/s/{token}" in page and "Drain hose" in page
        sid = shares_repo.get_public(conn, token)["id"]
        r = c.post(f"/shares/{sid}/revoke", headers={"hx-request": "true"})
        assert r.status_code == 200 and r.text == ""
        assert f"/s/{token}" not in c.get("/shares").text
        assert c.post("/shares/999999/revoke").status_code == 404
    with TestClient(app) as c:
        r = c.get(f"/s/{token}")
        assert r.status_code == 404 and "Drain hose" not in r.text
        assert r.json() == c.get("/s/not-a-real-token").json()


def test_script_in_content_is_escaped(conn):
    token = _share(conn, "<script>alert('t')</script>",
                   "Hi <script>alert(1)</script> <img src=x onerror=alert(2)>")
    with TestClient(app) as c:
        page = c.get(f"/s/{token}").text
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in page


def test_other_routes_still_require_auth(conn):
    token = _share(conn)
    with TestClient(app) as c:
        for path in ("/", "/shares", "/manuals", "/notes", "/search", "/manuals/1/file",
                     f"/s/{token}/x", f"/s/{token}/../../manuals/1/file", "/s/"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path
        assert c.post("/api/messages/1/share").status_code == 401
        assert c.post("/shares/1/revoke", headers={"hx-request": "true"}).status_code == 401


def test_share_button_snapshots_a_message(conn):
    cid = chat_repo.create_conversation(conn)
    chat_repo.add_message(conn, cid, "user", "What size is the drain hose?")
    mid = chat_repo.add_message(conn, cid, "assistant", "It fits **5/8 inch**.")
    uid = chat_repo.add_message(conn, cid, "user", "thanks")
    conn.commit()
    with TestClient(app) as c:
        _login(c)
        page = c.get(f"/c/{cid}").text
        assert f'data-id="{mid}"' in page and 'href="/shares"' in page
        r = c.post(f"/api/messages/{mid}/share", headers={"hx-request": "true"})
        assert r.status_code == 200 and "data-copy=" in r.text
        token = TOKEN_RE.search(r.text).group(1)
        assert f'href="http://testserver/s/{token}"' in r.text and f'data-copy="http://testserver/s/{token}"' in r.text
        assert c.post(f"/api/messages/{uid}/share").status_code == 404
        # A snapshot: changing the source message later doesn't change the page.
        conn.execute("UPDATE messages SET content = 'changed' WHERE id = ?", (mid,))
        conn.commit()
    with TestClient(app) as c:
        page = c.get(f"/s/{token}").text
        assert "What size is the drain hose?" in page and "**5/8 inch**" in page and "changed" not in page
