"""PDF -> per-page text with paragraph/heading structure, with OCR fallback."""
import io
import logging
import re
import shutil
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf as fitz

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 40
OCR_DPI = 300
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif"}


@dataclass
class Paragraph:
    text: str
    is_heading: bool = False


@dataclass
class Page:
    page_no: int
    text: str
    ocr: bool = False
    paragraphs: list[Paragraph] = field(default_factory=list)
    lang: str | None = None


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def images_to_pdf(image_paths: list[Path], out_path: Path) -> None:
    """Combine one or more photos into a single PDF (one image per page)."""
    out = fitz.open()
    for p in image_paths:
        img = fitz.open(p)
        pdf_bytes = img.convert_to_pdf()
        img.close()
        img_pdf = fitz.open("pdf", pdf_bytes)
        out.insert_pdf(img_pdf)
        img_pdf.close()
    out.save(out_path)
    out.close()


def page_count(path: Path) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


_ws = re.compile(r"[ \t]+")


def _clean(s: str) -> str:
    s = s.replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl")
    return _ws.sub(" ", s).strip()


def _looks_like_heading_text(s: str) -> bool:
    if not (3 <= len(s) <= 80):
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 3:
        return False
    if s.endswith((".", ",", ";")):
        return False
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    numbered = re.match(r"^(\d+(\.\d+)*|[A-Z])[.)]?\s+[A-Z]", s) is not None
    return upper_ratio > 0.8 or numbered


def _paragraphs_from_dict(page: "fitz.Page") -> list[Paragraph]:
    d = page.get_text("dict", sort=True)
    lines = []  # (text, max_size, bold)
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_lines = []
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = _clean(" ".join(s["text"] for s in spans))
            size = max(s.get("size", 0) for s in spans)
            bold = all((s.get("flags", 0) & 16) or "Bold" in s.get("font", "") for s in spans)
            block_lines.append((text, size, bold))
        if block_lines:
            lines.append(block_lines)
    sizes = [size for bl in lines for (t, size, _) in bl for _ in range(max(1, len(t) // 20))]
    body = statistics.median(sizes) if sizes else 10.0

    paras: list[Paragraph] = []
    for bl in lines:
        buf: list[str] = []
        for text, size, bold in bl:
            heading = (size >= body * 1.18 or (bold and size >= body * 0.98)) and _looks_like_heading_text(text) \
                or (size >= body * 1.35 and 2 <= len(text) <= 80)
            if heading:
                if buf:
                    paras.append(Paragraph(" ".join(buf)))
                    buf = []
                paras.append(Paragraph(text, is_heading=True))
            else:
                buf.append(text)
        if buf:
            paras.append(Paragraph(" ".join(buf)))
    return paras


def _paragraphs_from_plain(text: str) -> list[Paragraph]:
    paras = []
    for block in re.split(r"\n\s*\n", text):
        lines = [_clean(l) for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if len(lines) == 1 and _looks_like_heading_text(lines[0]):
            paras.append(Paragraph(lines[0], is_heading=True))
        else:
            paras.append(Paragraph(" ".join(lines)))
    return paras


def _ocr_page(page: "fitz.Page", langs: str) -> str:
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=OCR_DPI)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(img, lang=langs)


def extract_pages(path: Path, ocr_langs: str = "eng",
                  progress: Callable[[int, int], None] | None = None) -> list[Page]:
    pages: list[Page] = []
    can_ocr = ocr_available()
    warned = False
    with fitz.open(path) as doc:
        total = doc.page_count
        for i, page in enumerate(doc):
            paras = _paragraphs_from_dict(page)
            text = "\n\n".join(p.text for p in paras)
            used_ocr = False
            if len(text.strip()) < MIN_TEXT_CHARS:
                if can_ocr:
                    try:
                        ocr_text = _ocr_page(page, ocr_langs)
                        if len(ocr_text.strip()) > len(text.strip()):
                            paras = _paragraphs_from_plain(ocr_text)
                            text = "\n\n".join(p.text for p in paras)
                            used_ocr = True
                    except Exception as e:  # keep going; one bad page shouldn't kill the manual
                        log.warning("OCR failed on %s p.%d: %s", path.name, i + 1, e)
                elif not warned:
                    log.warning("tesseract not installed; scanned pages in %s will have no text", path.name)
                    warned = True
            pages.append(Page(page_no=i + 1, text=text, ocr=used_ocr, paragraphs=paras))
            if progress:
                progress(i + 1, total)
    return pages
