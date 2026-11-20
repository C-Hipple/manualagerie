from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_pdf


def client():
    return TestClient(app)


def test_requires_login():
    with client() as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.post("/api/conversations").status_code == 401
        assert c.get("/healthz").json() == {"ok": True}


def test_login_and_upload(tmp_path):
    with client() as c:
        assert c.post("/login", data={"password": "nope"}).status_code == 401
        r = c.post("/login", data={"password": "pw"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/").status_code == 200

        pdf = make_pdf(tmp_path / "Fridge.pdf", [[("FRIDGE FILTER", 16), ("Replace the water filter every six months.", 10)]])
        files = [("files", ("Fridge.pdf", pdf.read_bytes(), "application/pdf")),
                 ("files", ("notes.txt", b"hello", "text/plain"))]
        r = c.post("/manuals/upload", files=files, headers={"hx-request": "true"})
        assert "Added: Fridge.pdf" in r.text and "unsupported" in r.text
        r = c.post("/manuals/upload", files=files[:1], headers={"hx-request": "true"})
        assert "Already in library" in r.text
        assert "Fridge" in c.get("/manuals").text
        assert c.post("/api/conversations").json()["id"] > 0
