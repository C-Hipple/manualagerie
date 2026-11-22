import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repo import chat as chat_repo

URL = "/api/conversations/search"


@pytest.fixture(autouse=True)
def _clean(conn):
    yield
    conn.execute("DELETE FROM conversations")
    conn.commit()


@pytest.fixture
def web():
    with TestClient(app) as c:
        c.post("/login", data={"password": "pw"}, follow_redirects=False)
        yield c


def _conv(conn, title, *msgs):
    cid = chat_repo.create_conversation(conn, title)
    for role, content, kw in msgs:
        chat_repo.add_message(conn, cid, role, content, **kw)
    conn.commit()
    return cid


def test_finds_by_user_text(conn, web):
    cid = _conv(conn, "Dishwasher", ("user", "How do I descale the dishwasher?", {}))
    r = web.get(URL, params={"q": "descale"}, headers={"hx-request": "true"})
    assert r.status_code == 200
    assert f'href="/c/{cid}"' in r.text and "<mark>descale</mark>" in r.text


def test_finds_by_assistant_text(conn, web):
    cid = _conv(conn, "Furnace", ("user", "hello", {}),
                ("assistant", "Hold the reset button for five seconds.", {}))
    r = web.get(URL, params={"q": "reset"})
    assert f'href="/c/{cid}"' in r.text and "<mark>reset</mark>" in r.text


def test_ignores_tool_messages(conn, web):
    _conv(conn, "T", ("user", "question", {}),
          ("assistant", "searching zebrafish", {"tool_calls": [{"id": "1"}]}),
          ("tool", "zebrafish manual text", {"tool_call_id": "1"}))
    assert chat_repo.search_conversations(conn, "zebrafish") == []
    assert "No conversations match" in web.get(URL, params={"q": "zebrafish"}).text


@pytest.mark.parametrize("q", ['"', 'AND', 'OR NOT', 'foo*', '(', 'a" OR "b', 'col:val', '^x', '-',
                               'NEAR(a b)', '   ', ''])
def test_punctuation_and_operators_safe(conn, web, q):
    _conv(conn, "T", ("user", "plain words here", {}))
    assert web.get(URL, params={"q": q}).status_code == 200


def test_snippet_is_escaped(conn, web):
    _conv(conn, "T", ("user", "use <script>alert(1)</script> filter cartridge", {}))
    r = web.get(URL, params={"q": "cartridge"})
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text
    assert "<mark>cartridge</mark>" in r.text


def test_requires_auth():
    with TestClient(app) as c:
        assert c.get(URL, params={"q": "x"}, headers={"hx-request": "true"}).status_code == 401
        assert c.get(URL, params={"q": "x"}, follow_redirects=False).status_code == 401


def test_index_updated_on_delete(conn, web):
    cid = _conv(conn, "T", ("user", "unique quokka question", {}))
    assert chat_repo.search_conversations(conn, "quokka")
    web.post(f"/c/{cid}/delete", follow_redirects=False)
    assert chat_repo.search_conversations(conn, "quokka") == []
    assert conn.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'quokka'").fetchone()[0] == 0


def test_empty_query_lists_conversations(conn, web):
    cid = _conv(conn, "Listed one", ("user", "x", {}))
    r = web.get(URL, params={"q": ""})
    assert "Listed one" in r.text and f"/c/{cid}/delete" in r.text
