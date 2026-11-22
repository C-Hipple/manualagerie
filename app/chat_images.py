"""Screenshots and photos attached to chat questions: validate, normalise, store, and turn into LLM parts.

Uploads are identified by decoding them with Pillow (never by extension or Content-Type), downscaled so
the long side is at most MAX_SIDE, and re-encoded from pixels only, so EXIF (GPS, camera serials) and any
other metadata or trailing bytes are dropped. Files are stored under {DATA_DIR}/chat_images/.
"""
import base64
import io
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import get_settings

pillow_heif.register_heif_opener()  # iPhone HEIC/HEIF screenshots and photos

MAX_IMAGES = 4
MAX_UPLOAD_BYTES = 20 * 1024 * 1024     # raw upload, per image
MAX_PIXELS = 60_000_000                 # refuse decompression bombs before decoding
MAX_SIDE = 1600                         # long side after downscaling
MAX_STORED_BYTES = 1_500_000            # re-encoded file, per image
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "HEIF"}
EXT = {"image/jpeg": "jpg", "image/png": "png"}


class ImageRejected(ValueError):
    """The upload is not an image we accept; the message is safe to show the user."""


@dataclass
class ProcessedImage:
    data: bytes
    mime: str
    width: int
    height: int


def _encode(im: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def _flatten(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


def process(raw: bytes) -> ProcessedImage:
    """Validate and normalise one upload. Raises ImageRejected."""
    if not raw:
        raise ImageRejected("The file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageRejected(f"Images must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            w, h = probe.size
            probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ImageRejected("That file isn't a supported image (PNG, JPEG, WebP, GIF or HEIC).") from None
    if fmt not in ALLOWED_FORMATS:
        raise ImageRejected(f"{fmt} images aren't supported; use PNG, JPEG, WebP, GIF or HEIC.")
    if w * h > MAX_PIXELS:
        raise ImageRejected("That image is too large to process.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.seek(0)  # first frame of an animated GIF/WebP
            im.load()
            im = ImageOps.exif_transpose(im)
            # Screenshots (PNG/GIF, or anything with transparency) stay lossless; photos become JPEG.
            lossless = fmt in ("PNG", "GIF") or im.mode in ("RGBA", "LA", "P")
            im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            if lossless:
                out = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                data = _encode(out, "PNG", optimize=True)
                if len(data) <= MAX_STORED_BYTES:
                    return ProcessedImage(data, "image/png", *out.size)
            rgb = _flatten(im)
            for quality in (85, 75, 65, 50):
                data = _encode(rgb, "JPEG", quality=quality, optimize=True)
                if len(data) <= MAX_STORED_BYTES:
                    return ProcessedImage(data, "image/jpeg", *rgb.size)
            rgb.thumbnail((MAX_SIDE // 2, MAX_SIDE // 2), Image.Resampling.LANCZOS)
            data = _encode(rgb, "JPEG", quality=60, optimize=True)
            if len(data) > MAX_STORED_BYTES:
                raise ImageRejected("That image is too detailed to attach; try a smaller crop.")
            return ProcessedImage(data, "image/jpeg", *rgb.size)
    except ImageRejected:
        raise
    except Exception:
        raise ImageRejected("That image couldn't be read; it may be damaged.") from None


def path_for(filename: str) -> Path:
    return get_settings().chat_images_dir / filename


def save(conn: sqlite3.Connection, conv_id: int, msg_id: int, img: ProcessedImage) -> int:
    d = get_settings().chat_images_dir
    d.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.{EXT[img.mime]}"
    (d / filename).write_bytes(img.data)
    return conn.execute(
        "INSERT INTO message_images (message_id, conversation_id, filename, mime, width, height, bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, filename, img.mime, img.width, img.height, len(img.data)),
    ).lastrowid


def data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def image_parts(images: list[ProcessedImage]) -> list[dict]:
    """OpenAI-compatible content parts for the current turn."""
    return [{"type": "image_url", "image_url": {"url": data_url(i.mime, i.data)}} for i in images]
