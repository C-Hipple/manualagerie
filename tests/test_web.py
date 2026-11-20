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


def _login_and_upload(c, tmp_path, name="Fridge.pdf"):
    c.post("/login", data={"password": "pw"}, follow_redirects=False)
    pdf = make_pdf(tmp_path / name, [[("FRIDGE FILTER", 16), ("Replace the water filter.", 10)]])
    c.post("/manuals/upload", files=[("files", (name, pdf.read_bytes(), "application/pdf"))],
           headers={"hx-request": "true"})
    from app import db
    from app.repo import manuals as manuals_repo
    with db.get_conn() as conn:
        return dict(next(m for m in manuals_repo.list_all(conn) if m["filename"] == name))


def test_manual_tags_round_trip(tmp_path):
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Tagged.pdf")
        r = c.post(f"/manuals/{m['id']}", data={"tags": "downstairs AC, basement unit"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert 'value="downstairs AC, basement unit"' in c.get(f"/manuals/{m['id']}").text
        assert "downstairs AC" in c.get("/manuals/rows").text


def test_delete_manual_htmx(tmp_path):
    from app import db
    from app.config import get_settings
    from app.repo import manuals as manuals_repo
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Htmx.pdf")
        f = get_settings().files_dir / f"{m['sha256']}.pdf"
        assert f.exists()
        assert f'hx-post="/manuals/{m["id"]}/delete"' in c.get("/manuals/rows").text
        r = c.post(f"/manuals/{m['id']}/delete", headers={"hx-request": "true"})
        assert r.status_code == 200 and r.text == ""
        with db.get_conn() as conn:
            assert manuals_repo.get(conn, m["id"]) is None
        assert not f.exists()
        assert f'/manuals/{m["id"]}/delete' not in c.get("/manuals/rows").text


def test_delete_manual_form_and_unknown(tmp_path):
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Form.pdf")
        r = c.post(f"/manuals/{m['id']}/delete", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/manuals"
        assert c.post(f"/manuals/{m['id']}/delete").status_code == 404
        assert c.post("/manuals/999999/delete", headers={"hx-request": "true"}).status_code == 404
