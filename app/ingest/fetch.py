"""Server-side download of a manual from a URL, with SSRF guards."""
import hashlib
import ipaddress
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
CHUNK = 64 * 1024
SNIFF_BYTES = 1024
OK_CONTENT_TYPES = {"application/pdf", "application/x-pdf", "application/octet-stream", "binary/octet-stream"}
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


class FetchError(Exception):
    """Message is safe to show to the user."""


class FetchTooLarge(FetchError):
    pass


def _resolve(host: str, port: int) -> list[str]:
    return [i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _client() -> httpx.AsyncClient:
    # trust_env=False: ignore proxy env vars so the host we validated is the one we connect to.
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, trust_env=False)


def check_url(url: str) -> None:
    """Raise FetchError unless url is http(s) and its host resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("only http:// and https:// links are supported")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addrs = _resolve(parts.hostname, port)
    except (socket.gaierror, ValueError, UnicodeError):
        raise FetchError("could not resolve that host")
    if not addrs:
        raise FetchError("could not resolve that host")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise FetchError("that address is not allowed (private or internal network)")


def sniff_ext(head: bytes) -> str | None:
    """Detect the file type from magic bytes: 'pdf', an IMAGE_EXTS member, or None."""
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    if head[:2] == b"BM":
        return "bmp"
    if head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS:
        return "heic"
    return None


def _filename(url: str, ext: str) -> str:
    parts = urlsplit(url)
    name = unquote(parts.path.rsplit("/", 1)[-1]).strip()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem or parts.hostname or 'manual'}.{ext}"


async def _download(resp: httpx.Response, dest_dir: Path, max_bytes: int, tmp_name: str, url: str):
    """Stream resp into a temp file, aborting past max_bytes. Returns (path, sha, ext, filename)."""
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in OK_CONTENT_TYPES and not ctype.startswith("image/"):
        raise FetchError(f"not a PDF or image (Content-Type {ctype or 'missing'})")
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchTooLarge("file is too large")
    h = hashlib.sha256()
    size = 0
    head = b""
    out = None
    tmp: Path | None = None
    ext: str | None = None
    try:
        async for data in resp.aiter_bytes(CHUNK):
            size += len(data)
            if size > max_bytes:
                raise FetchTooLarge("file is too large")
            h.update(data)
            if out is not None:
                out.write(data)
                continue
            head += data
            if len(head) < SNIFF_BYTES:
                continue
            ext = sniff_ext(head)
            if ext is None:
                raise FetchError("not a PDF or supported image")
            tmp = dest_dir / f"{tmp_name}.{ext}"
            out = tmp.open("wb")
            out.write(head)
        if out is None:  # whole body was shorter than the sniff window
            ext = sniff_ext(head)
            if ext is None:
                raise FetchError("not a PDF or supported image")
            tmp = dest_dir / f"{tmp_name}.{ext}"
            tmp.write_bytes(head)
    except BaseException:
        if out is not None:
            out.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise
    if out is not None:
        out.close()
    return tmp, h.hexdigest(), ext, _filename(url, ext)


async def fetch_to_file(url: str, dest_dir: Path, max_bytes: int, tmp_name: str) -> tuple[Path, str, str, str]:
    """Download url (streaming, size-capped, SSRF-checked on every hop).

    Returns (tmp_path, sha256, ext, filename); ext comes from magic bytes, not the URL.
    """
    async with _client() as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError("redirect without a destination")
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code != 200:
                        raise FetchError(f"the server answered HTTP {resp.status_code}")
                    return await _download(resp, dest_dir, max_bytes, tmp_name, current)
            except httpx.TimeoutException:
                raise FetchError("the download timed out")
            except httpx.HTTPError:
                raise FetchError("could not download that link")
        raise FetchError("too many redirects")
