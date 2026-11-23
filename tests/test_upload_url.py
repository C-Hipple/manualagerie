import ipaddress

import httpx
import pillow_heif
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.config import get_settings
from app.ingest import extract, fetch
from app.main import app
from tests.conftest import login, make_pdf

PUBLIC = "93.184.216.34"


@pytest.fixture
def client():
    with TestClient(app) as c:
        login(c)
        yield c


@pytest.fixture
def serve(monkeypatch):
    """Fake network: serve(handler, resolve={host: ip}); handler receives an httpx.Request."""
    def install(handler, resolve=None):
        table = resolve or {}
        def resolve(host, port):  # like getaddrinfo: IP literals resolve to themselves
            try:
                return [str(ipaddress.ip_address(host))]
            except ValueError:
                return [table.get(host, PUBLIC)]

        monkeypatch.setattr(fetch, "_resolve", resolve)
        monkeypatch.setattr(fetch, "_client", lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False))
    return install


def post(client, url):
    return client.post("/manuals/upload-url", data={"url": url}, headers={"hx-request": "true"})


def pdf_bytes(tmp_path, name):
    return make_pdf(tmp_path / name, [[("URL MANUAL", 16), (f"Unique text for {name}", 10)]]).read_bytes()


def test_fetch_pdf_success_and_dedupe(client, serve, tmp_path):
    body = pdf_bytes(tmp_path, "oven.pdf")
    serve(lambda req: httpx.Response(200, content=body, headers={"content-type": "application/pdf"}))
    url = "https://files.example.com/docs/Oven%20Guide.pdf"
    assert "Added: Oven Guide.pdf" in post(client, url).text
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM manuals WHERE filename = 'Oven Guide.pdf'").fetchone()
        assert row["source_url"] == url
        assert conn.execute("SELECT COUNT(*) c FROM jobs WHERE manual_id = ?", (row["id"],)).fetchone()["c"] == 1
    assert "Already in library" in post(client, url).text


def test_redirect_followed_and_type_from_magic_bytes(client, serve, tmp_path):
    body = pdf_bytes(tmp_path, "redir.pdf")

    def handler(req):
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/download?id=7"})
        # no .pdf suffix and a generic content type: magic bytes decide
        return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})

    serve(handler)
    assert "Added: download.pdf" in post(client, "https://example.com/start").text


def test_private_ip_rejected(client, serve):
    calls = []
    serve(lambda req: calls.append(req) or httpx.Response(200), resolve={"internal.example.com": "10.0.0.5"})
    for url in ("http://internal.example.com/a.pdf", "http://127.0.0.1/a.pdf", "http://[::1]/a.pdf",
                "http://169.254.169.254/latest/meta-data", "http://[::ffff:10.0.0.1]/a.pdf"):
        assert "not allowed" in post(client, url).text, url
    assert not calls


def test_unsupported_scheme(client, serve):
    serve(lambda req: httpx.Response(200))
    assert "http:// and https://" in post(client, "file:///etc/passwd").text
    assert "http:// and https://" in post(client, "ftp://example.com/a.pdf").text


def test_redirect_to_private_ip_rejected(client, serve):
    def handler(req):
        if req.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://evil.internal/secret.pdf"})
        raise AssertionError("private host must never be requested")

    serve(handler, resolve={"evil.internal": "192.168.1.10"})
    assert "not allowed" in post(client, "https://public.example.com/x.pdf").text


def test_redirect_loop_limited(client, serve):
    hops = []

    def handler(req):
        hops.append(1)
        return httpx.Response(302, headers={"location": f"/n{len(hops)}"})

    serve(handler)
    assert "too many redirects" in post(client, "https://example.com/a").text
    assert len(hops) == fetch.MAX_REDIRECTS + 1


def test_oversize_aborts_early(client, serve, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    sent = []

    async def body():
        yield b"%PDF-1.4\n"
        for _ in range(100):  # 100 MB if it were not aborted
            sent.append(1)
            yield b"x" * (1024 * 1024)

    serve(lambda req: httpx.Response(200, content=body(), headers={"content-type": "application/pdf"}))
    assert "larger than 1 MB" in post(client, "https://example.com/huge.pdf").text
    assert len(sent) < 5
    assert not any(f.suffix == ".pdf" for f in get_settings().tmp_dir.iterdir())


def test_html_page_rejected(client, serve):
    serve(lambda req: httpx.Response(200, content=b"<html>nope</html>", headers={"content-type": "text/html"}))
    assert "not a PDF or image" in post(client, "https://example.com/manual.pdf").text


def test_wrong_bytes_rejected_despite_pdf_header(client, serve):
    serve(lambda req: httpx.Response(200, content=b"<html>" + b"a" * 3000, headers={"content-type": "application/pdf"}))
    assert "not a PDF or supported image" in post(client, "https://example.com/manual.pdf").text
    serve(lambda req: httpx.Response(200, content=b"tiny text", headers={"content-type": "application/pdf"}))
    assert "not a PDF or supported image" in post(client, "https://example.com/manual.pdf").text


def test_http_error_and_empty(client, serve):
    serve(lambda req: httpx.Response(404))
    assert "HTTP 404" in post(client, "https://example.com/a.pdf").text
    assert "Paste a link" in post(client, "  ").text


def make_heic(path, color=(200, 30, 30)):
    pillow_heif.register_heif_opener()
    Image.new("RGB", (64, 48), color).save(path, format="HEIF")
    return path


def test_heic_upload_becomes_pdf(client, tmp_path):
    assert {"heic", "heif"} <= extract.IMAGE_EXTS
    heic = make_heic(tmp_path / "IMG_0001.heic")
    r = client.post("/manuals/upload", files=[("files", ("IMG_0001.heic", heic.read_bytes(), "image/heic"))],
                    headers={"hx-request": "true"})
    assert "Added: IMG_0001.heic" in r.text
    out = tmp_path / "out.pdf"
    extract.images_to_pdf([heic], out)
    assert extract.page_count(out) == 1


def test_heic_from_url(client, serve, tmp_path):
    data = make_heic(tmp_path / "x.heic", (10, 120, 10)).read_bytes()
    assert fetch.sniff_ext(data[:1024]) == "heic"
    serve(lambda req: httpx.Response(200, content=data, headers={"content-type": "image/heic"}))
    assert "Added: photo.heic" in post(client, "https://example.com/photo.heic").text
