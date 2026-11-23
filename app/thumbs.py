"""Page thumbnails: rendered with PyMuPDF, cached as PNGs under {DATA_DIR}/thumbs/{manual_id}/{n}.png."""
import shutil
from pathlib import Path

import pymupdf as fitz

from .config import get_settings

THUMB_WIDTH = 320


def manual_dir(manual_id: int) -> Path:
    return get_settings().thumbs_dir / str(int(manual_id))


def thumb_path(manual_id: int, n: int) -> Path:
    return manual_dir(manual_id) / f"{int(n)}.png"


def get_or_render(manual_id: int, pdf: Path, n: int) -> Path | None:
    """Return the cached PNG for 1-based page n, rendering it first if needed. None if n is out of range."""
    out = thumb_path(manual_id, n)
    if out.exists():
        return out
    with fitz.open(pdf) as doc:
        if n < 1 or n > doc.page_count:
            return None
        page = doc[n - 1]
        zoom = THUMB_WIDTH / page.rect.width if page.rect.width else 1
        data = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)
    return out


def invalidate(manual_id: int) -> None:
    shutil.rmtree(manual_dir(manual_id), ignore_errors=True)
