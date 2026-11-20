import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.llm import agent, tools
from app.llm.prompts import TOOL_NOTE_NOTES, build_notes
from app.main import app
from app.repo import chat as chat_repo
from app.repo import notes as notes_repo

FILTERS = ("Upstairs AC filters", "16x25x1, MERV 11.\nTwo of them, in the hallway ceiling returns.", "upstairs AC, HVAC")


@pytest.fixture(autouse=True)
def _clean_notes(conn):
    """The test database is shared across modules; leave no notes behind."""
    yield
    conn.execute("DELETE FROM notes")
    conn.commit()


def _note(conn, title, body=None, tags=None):
    nid = notes_repo.create(conn, **notes_repo.clean(title, body, tags))
    conn.commit()
    return notes_repo.get(conn, nid)


def _login(c):
    c.post("/login", data={"password": "pw"}, follow_redirects=False)


@pytest.mark.parametrize("title, body, tags, expected", [
    ("  Upstairs  AC filters ", " 16x25x1 \r\n", " HVAC ;; hvac, upstairs ",
     {"title": "Upstairs AC filters", "body": "16x25x1", "tags": "HVAC, upstairs"}),
    ("", "Wi-Fi password is on the fridge\nsecond line", "",
     {"title": "Wi-Fi password is on the fridge", "body": "second line", "tags": None}),
    ("", "Water shut-off: basement, behind the furnace", "",
     {"title": "Water shut-off: basement, behind the furnace", "body": None, "tags": None}),
    ("Just a title", "", None, {"title": "Just a title", "body": None, "tags": None}),
    ("   ", "  \n ", "tag", None),
])
def test_clean(title, body, tags, expected):
    assert notes_repo.clean(title, body, tags) == expected


def test_clean_caps_long_titles():
    out = notes_repo.clean("x" * 500, "", "")
    assert len(out["title"]) == notes_repo.TITLE_MAX and out["title"].endswith("…")
    # A long first line that had to be clipped into a title stays in the body in full.
    out = notes_repo.clean("", "y" * 500, "")
    assert out["title"].endswith("…") and out["body"] == "y" * 500


def test_search_ranks_tags_then_title_then_body(conn):
    in_body = _note(conn, "Paint", "Hallway is SW 7008, the upstairs bath too")
    in_tags = _note(conn, *FILTERS)
    _note(conn, "Water shut-off", "Basement, behind the furnace")
    ids = [n["id"] for n in notes_repo.search(conn, "upstairs")]
    assert ids == [in_tags["id"], in_body["id"]]
    assert len(notes_repo.search(conn, "")) == 3


def test_migration_adds_notes_to_an_existing_library(tmp_path):
    from app import db
    old = db.connect(tmp_path / "old.db")
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql"))[:2]:
        old.executescript(f.read_text())
    old.execute("PRAGMA user_version = 2")
    old.execute("INSERT INTO manuals (sha256, filename) VALUES ('abc', 'old.pdf')")
    old.commit()
    assert db.migrate(old) >= 3
    assert old.execute("SELECT filename FROM manuals").fetchone()["filename"] == "old.pdf"
    old.execute("INSERT INTO notes (title) VALUES ('x')")
    assert old.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    old.close()


def test_notes_crud_pages(conn):
    with TestClient(app) as c:
        assert c.get("/notes", follow_redirects=False).status_code == 303  # login required
        _login(c)
        assert "No notes yet" in c.get("/notes").text
        assert 'href="/notes"' in c.get("/").text  # nav link

        r = c.post("/notes", data=dict(zip(("title", "body", "tags"), FILTERS)), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/notes#note-")
        nid = int(r.headers["location"].rsplit("-", 1)[1])
        page = c.get("/notes").text
        assert f'id="note-{nid}"' in page and "16x25x1, MERV 11." in page
        assert '<span class="tag">upstairs AC</span>' in page

        assert c.post("/notes", data={"title": " ", "body": ""}, follow_redirects=False).headers["location"] \
            == "/notes?empty=1"
        assert "Give the note a title" in c.get("/notes?empty=1").text

        edit = c.get(f"/notes/{nid}").text
        assert 'value="Upstairs AC filters"' in edit and "hallway ceiling returns" in edit
        r = c.post(f"/notes/{nid}", data={"title": "Upstairs AC filters", "body": "20x25x1 now", "tags": ""},
                   follow_redirects=False)
        assert r.status_code == 303
        n = notes_repo.get(conn, nid)
        assert n["body"] == "20x25x1 now" and n["tags"] is None
        # Blanking a note on the edit page keeps the old text instead of saving an empty note.
        r = c.post(f"/notes/{nid}", data={"title": "", "body": ""}, follow_redirects=False)
        assert r.headers["location"] == f"/notes/{nid}?empty=1"
        assert notes_repo.get(conn, nid)["body"] == "20x25x1 now"

        r = c.post(f"/notes/{nid}/delete", headers={"hx-request": "true"})
        assert r.status_code == 200 and r.text == ""
        assert notes_repo.get(conn, nid) is None
        assert c.post(f"/notes/{nid}/delete").status_code == 404
        assert c.get(f"/notes/{nid}").status_code == 404
        assert c.post(f"/notes/{nid}", data={"title": "x"}).status_code == 404


def test_system_prompt_includes_notes(conn):
    n = _note(conn, *FILTERS)
    prompt = agent.build_system_prompt(conn)
    assert f"[note #{n['id']}] Upstairs AC filters (tags: upstairs AC, HVAC)" in prompt
    assert "    16x25x1, MERV 11.\n    Two of them" in prompt
    assert "[note #<id>]" in prompt and TOOL_NOTE_NOTES.strip() not in prompt


def test_many_notes_are_listed_by_title_only(conn, monkeypatch):
    n = _note(conn, *FILTERS)
    text, abbreviated = build_notes([n], char_limit=10, line_limit=50)
    assert abbreviated and "Upstairs AC filters" in text and "16x25x1" not in text
    monkeypatch.setattr(agent.get_settings(), "notes_inline_chars", 10)
    prompt = agent.build_system_prompt(conn)
    assert TOOL_NOTE_NOTES.strip() in prompt and "16x25x1" not in prompt


def test_search_notes_tool(conn):
    n = _note(conn, *FILTERS)
    assert "search_notes" in [t["function"]["name"] for t in tools.TOOLS]
    out, _ = tools.dispatch(conn, "search_notes", json.dumps({"query": "upstairs filter"}))
    assert f"[note #{n['id']}]" in out and "16x25x1" in out
    out, _ = tools.dispatch(conn, "search_notes", json.dumps({"query": "garage door"}))
    assert out.startswith("No household notes matched")
    assert "notes" in tools.status_text("search_notes", {"query": "filter"}, conn)


def test_parse_note_citations():
    text = "16x25x1 [note #4], see [Note 4] and [note#9] but not [#4 p.2]"
    assert agent.parse_note_citations(text) == [{"note_id": 4}, {"note_id": 9}]


def test_answer_cites_a_note(conn, monkeypatch):
    n = _note(conn, *FILTERS)
    monkeypatch.setattr(agent.get_settings(), "openrouter_api_key", "fake")

    async def fake_stream(messages, *, tools, tool_choice):
        assert "16x25x1" in messages[0]["content"]
        text = f"Your upstairs AC takes 16x25x1 MERV 11 filters [note #{n['id']}]."
        yield "token", text
        yield "result", {"content": text, "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()

    async def collect():
        return [ev async for ev in agent.run_turn(cid, "What size are my upstairs AC filters?")]

    done = asyncio.run(collect())[-1]
    assert done["type"] == "done"
    assert done["citations"] == [{"note_id": n["id"], "title": "Upstairs AC filters"}]


def test_chat_page_knows_note_titles(conn):
    n = _note(conn, "Odd </script><b>title", "body")
    with TestClient(app) as c:
        _login(c)
        page = c.get("/").text
    assert 'id="note-titles"' in page and "and 1 note" in page
    assert "</script><b>" not in page
    blob = page.split('id="note-titles" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob) == {str(n["id"]): "Odd </script><b>title"}
