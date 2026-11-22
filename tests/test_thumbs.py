from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.test_web import _login_and_upload


def client():
    return TestClient(app)


def _cache(mid):
    return get_settings().thumbs_dir / str(mid)


def test_renders_and_caches(tmp_path):
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Thumb1.pdf")
        r = c.get(f"/manuals/{m['id']}/pages/1/thumb.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")
        assert "max-age" in r.headers["cache-control"]
        cached = _cache(m["id"]) / "1.png"
        assert cached.exists()
        cached.write_bytes(cached.read_bytes())  # served from cache afterwards
        mtime = cached.stat().st_mtime_ns
        assert c.get(f"/manuals/{m['id']}/pages/1/thumb.png").content == r.content
        assert cached.stat().st_mtime_ns == mtime


def test_out_of_range_and_unknown(tmp_path):
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Thumb2.pdf")
        for n in (0, 2, -1, 999):
            assert c.get(f"/manuals/{m['id']}/pages/{n}/thumb.png").status_code == 404
        assert c.get("/manuals/999999/pages/1/thumb.png").status_code == 404
        assert not (_cache(m["id"]) / "2.png").exists()


def test_requires_auth(tmp_path):
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Thumb3.pdf")
        c.post(f"/manuals/{m['id']}/reindex", follow_redirects=False)  # clears any cache left by earlier tests
    with client() as anon:
        r = anon.get(f"/manuals/{m['id']}/pages/1/thumb.png", follow_redirects=False)
        assert r.status_code in (303, 401)
        assert not _cache(m["id"]).exists()


def test_delete_and_reindex_invalidate(tmp_path):
    with client() as c:
        m = _login_and_upload(c, tmp_path, "Thumb4.pdf")
        c.get(f"/manuals/{m['id']}/pages/1/thumb.png")
        assert _cache(m["id"]).exists()
        c.post(f"/manuals/{m['id']}/reindex", follow_redirects=False)
        assert not _cache(m["id"]).exists()
        c.get(f"/manuals/{m['id']}/pages/1/thumb.png")
        assert _cache(m["id"]).exists()
        c.post(f"/manuals/{m['id']}/delete")
        assert not _cache(m["id"]).exists()
        assert c.get(f"/manuals/{m['id']}/pages/1/thumb.png").status_code == 404
