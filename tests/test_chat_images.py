import asyncio
import io
import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import chat_images
from app.config import get_settings
from app.llm import agent
from app.llm.prompts import IMAGE_ONLY_TEXT
from app.main import app
from app.repo import chat as chat_repo
from tests.test_ingest import _ingest
from tests.conftest import login


def _jpeg_with_exif(size=(3000, 2000), orientation=6) -> bytes:
    im = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation          # Orientation: rotate 90° CW to display
    exif[0x010F] = "SecretCam"          # Make
    exif[0x0131] = "GPS-leaky-firmware"  # Software
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=90)
    return buf.getvalue()


def _png(size=(400, 300), mode="RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (10, 120, 200, 128) if mode == "RGBA" else (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def _encoded(fmt: str, size=(500, 400)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 160, 90)).save(buf, fmt)
    return buf.getvalue()


def _bad_request(msg: str) -> openai.BadRequestError:
    resp = httpx.Response(400, request=httpx.Request("POST", "https://openrouter.test/chat/completions"))
    return openai.BadRequestError(msg, response=resp, body={"error": {"message": msg, "code": 400}})


def _run(cid, text, images=None):
    async def collect():
        return [ev async for ev in agent.run_turn(cid, text, images=images)]
    return asyncio.run(collect())


def _image_parts(content) -> list[dict]:
    return [p for p in content if p.get("type") == "image_url"] if isinstance(content, list) else []


# --- upload processing -------------------------------------------------------------------------

def test_upload_resized_rotated_and_exif_stripped():
    raw = _jpeg_with_exif()
    assert b"SecretCam" in raw
    img = chat_images.process(raw)
    assert img.mime == "image/jpeg"
    # Orientation 6 is applied to the pixels, then the long side is capped at 1600.
    assert (img.width, img.height) == (1067, 1600)
    assert len(img.data) <= chat_images.MAX_STORED_BYTES
    assert b"SecretCam" not in img.data and b"GPS-leaky" not in img.data
    with Image.open(io.BytesIO(img.data)) as out:
        assert out.format == "JPEG" and out.size == (1067, 1600)
        assert len(out.getexif()) == 0 and "exif" not in out.info


def test_screenshots_stay_png_and_other_formats_are_accepted():
    png = chat_images.process(_png())
    assert png.mime == "image/png" and (png.width, png.height) == (400, 300)
    for fmt in ("WEBP", "GIF"):
        out = chat_images.process(_encoded(fmt))
        assert out.mime in ("image/jpeg", "image/png") and (out.width, out.height) == (500, 400)


def test_heic_is_accepted():
    try:
        raw = _encoded("HEIF")
    except Exception as e:  # pragma: no cover - only if this libheif build has no encoder
        pytest.skip(f"cannot encode HEIF here: {e}")
    out = chat_images.process(raw)
    assert out.mime == "image/jpeg" and (out.width, out.height) == (500, 400)


@pytest.mark.parametrize("raw", [
    b"",
    b"hello, I am definitely a PNG",
    b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF",
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,  # PNG signature, garbage body
])
def test_non_images_rejected(raw):
    with pytest.raises(chat_images.ImageRejected):
        chat_images.process(raw)


def test_unsupported_image_formats_rejected():
    for fmt in ("BMP", "TIFF"):
        with pytest.raises(chat_images.ImageRejected, match="aren't supported"):
            chat_images.process(_encoded(fmt))


# --- agent ---------------------------------------------------------------------------------------

def test_current_turn_sends_image_parts_and_tool_loop_works(conn, pdf_path, monkeypatch):
    mid = _ingest(conn, pdf_path)
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "fake")
    calls = []

    async def fake_stream(messages, *, tools, tool_choice):
        calls.append(json.loads(json.dumps(messages)))
        if len(calls) == 1:
            yield "result", {"content": "", "usage": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "search_manuals", "arguments": json.dumps({"query": "drain hose"})}}]}
        else:
            assert messages[-1]["role"] == "tool"
            text = f"That's the drain hose; it fits 5/8\" to 1\" [#{mid} p.3]."
            yield "token", text
            yield "result", {"content": text, "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()
    events = _run(cid, "What is this hose?", [chat_images.process(_png())])
    assert events[-1]["type"] == "done", events

    user = calls[0][-1]
    assert user["role"] == "user" and isinstance(user["content"], list)
    assert user["content"][0] == {"type": "text", "text": "What is this hose?"}
    parts = _image_parts(user["content"])
    assert len(parts) == 1 and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # The second round (after the tool call) still carries the image on the user turn.
    assert len(_image_parts(calls[1][-3]["content"])) == 1
    assert "Photos and screenshots" in calls[0][0]["content"]

    rows = chat_repo.get_messages(conn, cid)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0]["content"] == "What is this hose?"  # stored as text, images in message_images
    imgs = chat_repo.images_by_message(conn, cid)[rows[0]["id"]]
    assert len(imgs) == 1 and chat_images.path_for(imgs[0]["filename"]).is_file()


def test_history_replaces_earlier_images_with_marker(conn, monkeypatch):
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "fake")
    calls = []

    async def fake_stream(messages, *, tools, tool_choice):
        calls.append(messages)
        yield "result", {"content": "It shows error E24, a drain fault.", "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()
    _run(cid, "", [chat_images.process(_png()), chat_images.process(_png())])
    _run(cid, "How do I fix that?")

    # Image-only turn: the text part asks the model to describe what it sees.
    assert calls[0][-1]["content"][0]["text"] == IMAGE_ONLY_TEXT
    assert chat_repo.get_conversation(conn, cid)["title"] == "Photo question"
    second = calls[1]
    assert second[-1] == {"role": "user", "content": "How do I fix that?"}
    prior_user = second[1]
    assert prior_user["role"] == "user" and prior_user["content"] == "[2 images previously attached]"
    assert second[2] == {"role": "assistant", "content": "It shows error E24, a drain fault."}
    assert not any(isinstance(m["content"], list) for m in second), "no images resent from history"

    rows = chat_repo.get_messages(conn, cid)
    hist = chat_repo.to_llm_history(rows[:1], {rows[0]["id"]: 1})
    assert hist == [{"role": "user", "content": chat_repo.IMAGE_MARKER}]
    assert chat_repo.with_image_marker("see photo", 1) == "see photo\n[image previously attached]"


def test_fallback_planner_text_only_answer_gets_images(conn, pdf_path, monkeypatch):
    mid = _ingest(conn, pdf_path)
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_api_key", "fake")
    monkeypatch.setattr(s, "llm_tools", False)
    plans, answers = [], []

    class FakeCompletions:
        async def create(self, **kw):
            plans.append(kw["messages"])
            msg = type("M", (), {"content": json.dumps({"queries": ["drain hose"], "manual_ids": [mid]})})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})

    async def fake_stream(messages, *, tools, tool_choice):
        assert tools is None
        answers.append(messages)
        yield "result", {"content": f"Drain hose [#{mid} p.3].", "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "get_async_client", lambda: FakeClient())
    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    cid = chat_repo.create_conversation(conn)
    conn.commit()
    events = _run(cid, "what hose is this?", [chat_images.process(_png())])
    assert events[-1]["type"] == "done", events
    planner_msgs = plans[0]
    assert all(isinstance(m["content"], str) for m in planner_msgs)
    assert "what hose is this?" in planner_msgs[0]["content"] and "base64" not in planner_msgs[0]["content"]
    assert len(_image_parts(answers[0][-1]["content"])) == 1


@pytest.mark.parametrize("tools_on", [True, False])
def test_unsupported_model_error_is_inline(conn, monkeypatch, tools_on):
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_api_key", "fake")
    monkeypatch.setattr(s, "llm_tools", tools_on)

    async def fake_stream(messages, *, tools, tool_choice):
        raise _bad_request("No endpoints found that support image input")
        yield  # pragma: no cover

    class FailingPlanner:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                async def create(**kw):
                    raise _bad_request("No endpoints found that support image input")

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    monkeypatch.setattr(agent, "get_async_client", lambda: FailingPlanner())
    cid = chat_repo.create_conversation(conn)
    conn.commit()
    events = _run(cid, "what is this?", [chat_images.process(_png())])
    assert events[-1]["type"] == "error", events
    assert "can't read images" in events[-1]["text"] and "OPENROUTER_MODEL" in events[-1]["text"]
    assert [e for e in events if e["type"] == "done"] == []


# --- web: auth, upload, rendering, delete, shares --------------------------------------------------

def _post(c, cid, text, *blobs):
    files = [("images", (f"shot{i}.png", b, "image/png")) for i, b in enumerate(blobs)]
    return c.post(f"/api/conversations/{cid}/messages", data={"text": text}, files=files)


def test_web_flow_auth_render_share_and_delete(monkeypatch):
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "fake")

    async def fake_stream(messages, *, tools, tool_choice):
        assert len(_image_parts(messages[-1]["content"])) == 2
        yield "token", "It's an E24 drain error."
        yield "result", {"content": "It's an E24 drain error.", "tool_calls": [], "usage": None}

    monkeypatch.setattr(agent, "_stream_completion", fake_stream)
    with TestClient(app) as c:
        login(c)
        cid = c.post("/api/conversations").json()["id"]
        r = _post(c, cid, "What does this code mean?", _jpeg_with_exif(), _png())
        assert r.status_code == 200 and '"type": "done"' in r.text, r.text

        from app import db
        with db.get_conn() as conn:
            rows = chat_repo.get_messages(conn, cid)
            imgs = chat_repo.images_by_message(conn, cid)[rows[0]["id"]]
        assert [i["mime"] for i in imgs] == ["image/jpeg", "image/png"]
        files = [chat_images.path_for(i["filename"]) for i in imgs]
        assert all(f.is_file() and f.parent == get_settings().chat_images_dir for f in files)

        page = c.get(f"/c/{cid}").text
        assert f'src="/api/chat-images/{imgs[0]["id"]}"' in page and 'id="attach"' in page
        got = c.get(f"/api/chat-images/{imgs[0]['id']}")
        assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
        assert got.content == files[0].read_bytes()

        # Shares snapshot the answer text only: the public page carries no images.
        share = c.post(f"/api/messages/{rows[1]['id']}/share")
        token = share.text.split("/s/")[1].split('"')[0].split("<")[0]
        with TestClient(app) as anon:
            pub = anon.get(f"/s/{token}")
            assert pub.status_code == 200 and "E24" in pub.text
            assert "chat-images" not in pub.text and "<img" not in pub.text and "base64" not in pub.text
            # Images require auth.
            assert anon.get(f"/api/chat-images/{imgs[0]['id']}").status_code == 401

        # Deleting the conversation deletes the files and rows.
        assert c.post(f"/c/{cid}/delete", follow_redirects=False).status_code == 303
        assert not any(f.exists() for f in files)
        assert c.get(f"/api/chat-images/{imgs[0]['id']}").status_code == 404


def test_web_rejects_bad_uploads(monkeypatch):
    with TestClient(app) as c:
        login(c)
        cid = c.post("/api/conversations").json()["id"]
        r = _post(c, cid, "what is this", b"not an image at all")
        assert r.status_code == 400 and "supported image" in r.json()["detail"]
        r = _post(c, cid, "too many", *[_png((20, 20))] * 5)
        assert r.status_code == 400 and "at most 4" in r.json()["detail"]
        assert c.post(f"/api/conversations/{cid}/messages", data={"text": "  "}).status_code == 400
        monkeypatch.setattr(get_settings(), "chat_images", False)
        r = _post(c, cid, "hi", _png((20, 20)))
        assert r.status_code == 400 and "CHAT_IMAGES" in r.json()["detail"]
        assert 'id="attach"' not in c.get("/").text
        from app import db
        with db.get_conn() as conn:
            assert chat_repo.get_messages(conn, cid) == []
