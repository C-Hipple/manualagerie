"""Read scanned pages, tables and diagrams with the LLM's vision instead of (or after) Tesseract.

`apply_vision` runs in the ingest worker after `extract_pages`. It picks the pages that need it
(see `needs_vision`), renders each one with PyMuPDF, asks the vision model for a markdown
transcription and swaps the text in. Any failure keeps the Tesseract / text-layer result:
vision never fails an ingest.
"""
import base64
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openai
import pymupdf as fitz

from ..config import Settings, get_settings
from .extract import MIN_TEXT_CHARS, Page, Paragraph, _clean, _looks_like_heading_text

log = logging.getLogger(__name__)

VISION_DPI = 150            # enough for small print on a letter page
VISION_MAX_PX = 2000        # longest side, so huge scans and posters stay a sane size
SHORT_OCR_CHARS = 80        # Tesseract output shorter than this counts as "very short"
LOW_OCR_CONF = 60.0         # mean word confidence (0-100) below this counts as unreliable
LOW_TEXT_COVERAGE = 0.04    # word boxes covering < 4% of a scanned page: it is mostly picture
MOSTLY_IMAGE_RATIO = 0.5    # embedded images covering >= half of a text-layer page ...
MOSTLY_IMAGE_MAX_CHARS = 500  # ... that has little text of its own (a diagram with a caption)
MAX_BLOCK_WORDS = 300       # keep long tables in row groups so chunking does not flatten them

PROMPT = """This image is one page of a household appliance or device manual. Transcribe it faithfully as markdown.

- Copy all text exactly, in its original language and reading order. Do not summarise, translate or add commentary.
- Use markdown headings (#, ##) for headings and keep lists as lists.
- Write every table as a markdown table, keeping all rows, columns and units.
- For each diagram, wiring diagram, exploded parts view or illustration, write a short description \
starting with "Diagram:" that lists every label, callout number, part number, wire colour, terminal \
name and measurement shown, and what each one points to or connects to.
- If the page is blank, reply with nothing.
Reply with the markdown only."""


def is_image_only(page: Page) -> bool:
    """No usable PDF text layer: a scan or a photo upload."""
    return page.native_chars < MIN_TEXT_CHARS


def needs_vision(page: Page, mode: str) -> bool:
    if mode == "off":
        return False
    if is_image_only(page):
        if mode == "always":
            return True
        if page.method != "tesseract" or len(page.text.strip()) < SHORT_OCR_CHARS:
            return True  # Tesseract missing, failed, or found next to nothing
        if page.ocr_conf is not None and page.ocr_conf < LOW_OCR_CONF:
            return True
        return page.ocr_coverage is not None and page.ocr_coverage < LOW_TEXT_COVERAGE
    # a text-layer page that is mostly a picture (diagram, exploded view) with a caption
    return page.image_ratio >= MOSTLY_IMAGE_RATIO and page.native_chars < MOSTLY_IMAGE_MAX_CHARS


def render_page_png(page: "fitz.Page", dpi: int = VISION_DPI, max_px: int = VISION_MAX_PX) -> bytes:
    zoom = dpi / 72
    longest = max(page.rect.width, page.rect.height) * zoom
    if longest > max_px:
        zoom *= max_px / longest
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


def build_messages(png: bytes) -> list[dict]:
    url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]


_fence = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_fence(s: str) -> str:
    s = s.strip()
    m = _fence.match(s)
    return m.group(1).strip() if m else s


def paragraphs_from_markdown(md: str) -> list[Paragraph]:
    """Like extract._paragraphs_from_plain, but keeps line breaks so tables and lists survive."""
    paras: list[Paragraph] = []
    for block in re.split(r"\n\s*\n", md):
        lines = [l.rstrip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if len(lines) == 1:
            heading = re.match(r"^#{1,6}\s+(.*)$", lines[0])
            if heading or _looks_like_heading_text(_clean(lines[0])):
                paras.append(Paragraph(_clean(heading.group(1) if heading else lines[0]).strip("*"),
                                       is_heading=True))
                continue
        buf: list[str] = []
        for line in lines:
            buf.append(line)
            if sum(len(l.split()) for l in buf) >= MAX_BLOCK_WORDS:
                paras.append(Paragraph("\n".join(buf)))
                buf = []
        if buf:
            paras.append(Paragraph("\n".join(buf)))
    return paras


def transcribe(client, model: str, png: bytes) -> tuple[str, dict | None]:
    resp = client.chat.completions.create(model=model, messages=build_messages(png),
                                          temperature=0, max_tokens=4096)
    usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
    return _strip_fence(resp.choices[0].message.content or ""), usage


def apply_vision(path: Path, pages: list[Page], settings: Settings | None = None, client=None) -> int:
    """Re-read the pages that need it with the vision model. Returns how many pages it replaced."""
    s = settings or get_settings()
    if s.vision_ocr == "off" or not s.openrouter_api_key:
        return 0
    todo = [p for p in pages if needs_vision(p, s.vision_ocr)]
    if not todo:
        return 0
    if client is None:
        from ..llm.client import get_client  # shares the SDK's 429/5xx retry-with-backoff settings
        client = get_client()

    rate_limited = threading.Event()
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    lock = threading.Lock()        # usage totals
    doc_lock = threading.Lock()    # PyMuPDF documents are not thread-safe

    def run(page: Page) -> tuple[Page, str | None]:
        if rate_limited.is_set():
            return page, None  # the SDK already retried; stop hammering and keep Tesseract text
        try:
            with doc_lock:
                png = render_page_png(doc[page.page_no - 1])
            text, usage = transcribe(client, s.vision_model, png)
        except openai.RateLimitError as e:
            rate_limited.set()
            log.warning("vision OCR rate limited on %s p.%d; using Tesseract for the rest: %s",
                        path.name, page.page_no, e)
            return page, None
        except Exception as e:
            log.warning("vision OCR failed on %s p.%d; keeping %s text: %s",
                        path.name, page.page_no, page.method, e)
            return page, None
        if usage:
            with lock:
                totals["prompt_tokens"] += usage.get("prompt_tokens") or 0
                totals["completion_tokens"] += usage.get("completion_tokens") or 0
            log.info("vision OCR %s p.%d model=%s usage=%s", path.name, page.page_no, s.vision_model, usage)
        return page, text

    replaced = 0
    with fitz.open(path) as doc, ThreadPoolExecutor(max_workers=max(1, s.vision_concurrency), thread_name_prefix="vision-ocr") as pool:
        for page, text in pool.map(run, todo):
            if not text or not text.strip():
                continue
            page.paragraphs = paragraphs_from_markdown(text)
            page.text = "\n\n".join(p.text for p in page.paragraphs)
            page.method = "vision"
            page.ocr = True
            replaced += 1
    log.info("vision OCR %s: %d/%d pages read with vision, model=%s prompt_tokens=%d completion_tokens=%d",
             path.name, replaced, len(todo), s.vision_model, totals["prompt_tokens"], totals["completion_tokens"])
    return replaced
