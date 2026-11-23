"""Vision OCR: every test uses a fake client, nothing touches the network."""
import base64
import hashlib
import shutil
from types import SimpleNamespace

import httpx
import openai
import pymupdf as fitz
import pytest

from app.config import Settings, get_settings
from app.ingest import vision
from app.ingest.extract import Page, Paragraph, extract_pages
from app.ingest.worker import process_manual
from app.repo import manuals as manuals_repo
from app.search import fts
from tests.conftest import login

TABLE_MD = """```markdown
# WIRING DIAGRAM

| Terminal | Wire colour | Gauge |
|---|---|---|
| L1 | Black | 14 AWG |
| N | White | 14 AWG |

Diagram: control board, terminal L1 connects to the black wire, part no. WB27X10489, 3/8 in clearance.
```"""


class FakeClient:
    def __init__(self, reply=TABLE_MD, error: Exception | None = None):
        self.reply, self.error, self.calls = reply, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 1100, "completion_tokens": 90})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))], usage=usage)


def _settings(mode="fallback", key="sk-test", **kw) -> Settings:
    return Settings(openrouter_api_key=key, vision_ocr=mode, openrouter_model="main/model", **kw)


def _scan_pdf(path, pages=1):
    """A PDF whose pages are only an image (like a scan): no text layer."""
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 80), False)
    pix.set_rect(pix.irect, (230, 230, 230))
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()
    return path


def _scan_page(no=1, text="", conf=None, coverage=None, method=None):
    paras = [Paragraph(text)] if text else []
    return Page(page_no=no, text=text, paragraphs=paras, ocr=bool(text),
                method=method or ("tesseract" if text else "text"),
                native_chars=0, image_ratio=1.0, ocr_conf=conf, ocr_coverage=coverage)


GOOD_OCR = "Connect the water inlet using a 3/8 inch compression fitting. " * 4


def test_fallback_triggers_on_empty_ocr(tmp_path):
    path = _scan_pdf(tmp_path / "scan.pdf")
    pages = [_scan_page(text="")]
    client = FakeClient()
    assert vision.apply_vision(path, pages, _settings(), client) == 1
    assert len(client.calls) == 1
    assert pages[0].method == "vision"
    assert "| L1 | Black | 14 AWG |" in pages[0].text  # table lines survive
    assert "WB27X10489" in pages[0].text
    assert pages[0].paragraphs[0] == Paragraph("WIRING DIAGRAM", is_heading=True)


def test_fallback_decisions():
    assert vision.needs_vision(_scan_page(text=""), "fallback")
    assert vision.needs_vision(_scan_page(text="3/8 in"), "fallback")                 # very short
    assert vision.needs_vision(_scan_page(text=GOOD_OCR, conf=35, coverage=0.3), "fallback")  # low confidence
    assert vision.needs_vision(_scan_page(text=GOOD_OCR, conf=90, coverage=0.01), "fallback")  # mostly picture
    assert not vision.needs_vision(_scan_page(text=GOOD_OCR, conf=90, coverage=0.3), "fallback")
    assert vision.needs_vision(_scan_page(text=GOOD_OCR, conf=90, coverage=0.3), "always")
    text_page = Page(page_no=1, text=GOOD_OCR, native_chars=len(GOOD_OCR), image_ratio=0.0)
    assert not vision.needs_vision(text_page, "fallback")
    assert not vision.needs_vision(text_page, "always")
    diagram_page = Page(page_no=1, text="Fig. 3 exploded view", native_chars=120, image_ratio=0.8)
    assert vision.needs_vision(diagram_page, "fallback")


def test_off_never_calls_client(tmp_path):
    path = _scan_pdf(tmp_path / "scan.pdf")
    pages = [_scan_page(text="")]
    client = FakeClient()
    assert vision.apply_vision(path, pages, _settings(mode="off"), client) == 0
    assert client.calls == []
    assert pages[0].method == "text"


def test_no_api_key_skips_silently(tmp_path):
    path = _scan_pdf(tmp_path / "scan.pdf")
    pages = [_scan_page(text="")]
    client = FakeClient()
    assert vision.apply_vision(path, pages, _settings(key=""), client) == 0
    assert client.calls == []


@pytest.mark.parametrize("error", [
    RuntimeError("boom"),
    openai.RateLimitError("slow down", body=None, response=httpx.Response(
        429, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))),
])
def test_client_error_keeps_tesseract_text(tmp_path, error):
    path = _scan_pdf(tmp_path / "scan.pdf", pages=2)
    pages = [_scan_page(1, text=GOOD_OCR, conf=20, coverage=0.3), _scan_page(2, text=GOOD_OCR, conf=20, coverage=0.3)]
    client = FakeClient(error=error)
    assert vision.apply_vision(path, pages, _settings(vision_concurrency=1), client) == 0
    for p in pages:
        assert p.text == GOOD_OCR
        assert p.method == "tesseract"
    if isinstance(error, openai.RateLimitError):
        assert len(client.calls) == 1  # stops after the SDK's own retries gave up


def test_request_contains_image_url_part(tmp_path):
    path = _scan_pdf(tmp_path / "scan.pdf")
    client = FakeClient()
    vision.apply_vision(path, [_scan_page(text="")], _settings(openrouter_vision_model="vision/model"), client)
    call = client.calls[0]
    assert call["model"] == "vision/model"
    parts = call["messages"][0]["content"]
    assert any(p["type"] == "text" and "markdown table" in p["text"] for p in parts)
    image = next(p for p in parts if p["type"] == "image_url")
    url = image["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_vision_model_defaults():
    assert _settings().vision_model == "main/model"
    assert _settings(openrouter_metadata_model="meta/model").vision_model == "meta/model"


def test_render_caps_pixel_size(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=2000, height=3000)  # a poster-sized page
    png = vision.render_page_png(page)
    pix = fitz.Pixmap(png)
    assert max(pix.width, pix.height) <= vision.VISION_MAX_PX
    doc.close()


def test_extract_marks_scanned_page_image_only(tmp_path):
    pages = extract_pages(_scan_pdf(tmp_path / "scan.pdf"))
    assert vision.is_image_only(pages[0])
    assert pages[0].image_ratio > 0.9


def test_method_is_recorded(conn, tmp_path, monkeypatch):
    path = tmp_path / "furnace-wiring.pdf"
    doc = fitz.open()
    doc.new_page().insert_textbox(fitz.Rect(72, 72, 540, 400),
                                  "FURNACE INSTALLATION. Mount the furnace on a level surface and keep "
                                  "the clearances listed on the rating plate.", fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()
    _scan_pdf(tmp_path / "scan.pdf")
    with fitz.open(path) as d, fitz.open(tmp_path / "scan.pdf") as scan:
        d.insert_pdf(scan)
        d.save(tmp_path / "manual.pdf")
    path = tmp_path / "manual.pdf"

    s = get_settings()
    monkeypatch.setattr(s, "openrouter_api_key", "sk-test")
    monkeypatch.setattr(s, "vision_ocr", "fallback")
    client = FakeClient()
    monkeypatch.setattr("app.llm.client.get_client", lambda: client)
    monkeypatch.setattr("app.ingest.worker.extract_metadata", lambda *a, **k: {"title": "Furnace"})

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    shutil.copy(path, s.files_dir / f"{sha}.pdf")
    mid = manuals_repo.create(conn, sha, path.name, "pdf")
    conn.commit()
    process_manual(conn, mid)

    assert len(client.calls) == 1
    methods = dict(conn.execute("SELECT page_no, ocr_method FROM pages WHERE manual_id = ?", (mid,)).fetchall())
    assert methods == {1: "text", 2: "vision"}
    assert manuals_repo.page_stats(conn, mid)["vision"] == 1
    assert manuals_repo.get(conn, mid)["status"] == "ready"
    hits = fts.search(conn, "WB27X10489", [mid], 5)
    assert hits and hits[0]["page_start"] <= 2 <= hits[0]["page_end"]


def test_detail_page_shows_vision_count(conn):
    from fastapi.testclient import TestClient
    from app.main import app

    sha = hashlib.sha256(b"vision-detail-page").hexdigest()
    mid = manuals_repo.create(conn, sha, "boiler.pdf", "pdf")
    manuals_repo.replace_content(conn, mid, [
        {"page_no": 1, "text": "a", "ocr": False, "ocr_method": "text"},
        {"page_no": 2, "text": "b", "ocr": True, "ocr_method": "vision"},
        {"page_no": 3, "text": "c", "ocr": True, "ocr_method": "vision"},
        {"page_no": 4, "text": "d", "ocr": True, "ocr_method": "tesseract"},
    ], [])
    conn.commit()
    try:
        with TestClient(app) as c:
            login(c)
            html = c.get(f"/manuals/{mid}").text
    finally:  # the DB is shared across the test session; don't leave a manual without a file behind
        manuals_repo.delete(conn, mid)
        conn.commit()
    assert "2 pages read with vision" in html
    assert "1 pages OCR&#39;d" in html or "1 pages OCR'd" in html
