import io
import json
import sqlite3
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db, library_io
from app.config import get_settings
from app.ingest import worker
from app.main import app
from app.repo import manuals as manuals_repo
from tests.conftest import make_pdf
from tests.conftest import login


@pytest.fixture
def c():
    with TestClient(app) as client:
        login(client)
        yield client


def _upload(c, tmp_path, name, text):
    pdf = make_pdf(tmp_path / name, [[(text, 16)]])
    r = c.post("/manuals/upload", files=[("files", (name, pdf.read_bytes(), "application/pdf"))],
               headers={"hx-request": "true"})
    assert "Added" in r.text, r.text
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM manuals WHERE filename = ?", (name,)).fetchone())


def _state():
    """Everything a round trip must preserve: manual rows (id, sha, tags) and stored files."""
    with db.get_conn() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT id, sha256, filename, tags FROM manuals ORDER BY id")]
    files_dir = get_settings().files_dir
    files = {p.relative_to(files_dir).as_posix(): p.read_bytes() for p in files_dir.rglob("*") if p.is_file()}
    return rows, files


def _export(c) -> bytes:
    r = c.get("/settings/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    return r.content


def _import(c, data: bytes, confirm="REPLACE"):
    return c.post("/settings/import", data={"confirm": confirm},
                  files={"archive": ("library.zip", data, "application/zip")})


def _rezip(data: bytes, *, drop=(), replace=None, add=None) -> bytes:
    """Copy an export, dropping/replacing/adding entries."""
    replace, add = replace or {}, add or {}
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            dst.writestr(info.filename, replace.get(info.filename, src.read(info)))
        for name, content in add.items():
            dst.writestr(name, content)
    return out.getvalue()


def test_settings_page_in_nav(c):
    assert 'href="/settings"' in c.get("/manuals").text
    page = c.get("/settings").text
    assert 'action="/settings/import"' in page and 'href="/settings/export"' in page and "REPLACE" in page


def test_auth_required():
    with TestClient(app) as anon:
        before = _state()
        r = anon.get("/settings/export", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert anon.get("/settings", follow_redirects=False).status_code == 303
        r = anon.post("/settings/import", data={"confirm": "REPLACE"},
                      files={"archive": ("x.zip", b"PK", "application/zip")}, follow_redirects=False)
        assert r.status_code == 303
        assert anon.get("/settings/export", headers={"hx-request": "true"}).status_code == 401
        assert _state() == before


def test_export_contents_and_no_secret(c, tmp_path):
    s = get_settings()
    secret = s.data_dir / "secret_key"
    secret.write_text("super-secret-cookie-key")
    (s.backups_dir / "manualagerie-20000101-000000.db").write_bytes(b"old backup")
    (s.data_dir / "thumbs").mkdir(exist_ok=True)
    (s.data_dir / "thumbs" / "1-1.png").write_bytes(b"thumb")
    (s.tmp_dir / "half-upload.pdf").write_bytes(b"tmp")
    try:
        m = _upload(c, tmp_path, "Exported.pdf", "EXPORT ME")
        data = _export(c)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            assert "manualagerie.db" in names and f"files/{m['sha256']}.pdf" in names
            assert all(n in ("manifest.json", "manualagerie.db") or n.startswith("files/") for n in names)
            for n in names:
                assert b"super-secret-cookie-key" not in zf.read(n)
            snap = tmp_path / "snap.db"
            snap.write_bytes(zf.read("manualagerie.db"))
        assert manifest["format"] == "manualagerie-export"
        assert manifest["schema_version"] == library_io.schema_version() >= 11
        assert manifest["exported_at"]
        assert manifest["counts"]["manuals"] >= 1 and manifest["counts"]["files"] >= 1
        conn = sqlite3.connect(snap)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM manuals WHERE sha256 = ?", (m["sha256"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM login_attempts").fetchone()[0] == 0
        conn.close()
        assert not list(s.tmp_dir.glob("export-*")), "temp export files must be cleaned up"
    finally:
        secret.unlink(missing_ok=True)


@pytest.mark.parametrize("with_worker", [False, True])
def test_round_trip_restores_original_state(c, tmp_path, with_worker):
    if with_worker:
        worker.start()  # the import must pause it, swap underneath it, and resume it
    try:
        _round_trip(c, tmp_path, f"Keep{int(with_worker)}.pdf")
        if with_worker:
            assert worker._thread.is_alive()
    finally:
        if with_worker:
            worker.stop()


def _round_trip(c, tmp_path, name):
    keep = _upload(c, tmp_path, name, f"KEEP THIS MANUAL {name}")
    c.post(f"/manuals/{keep['id']}", data={"tags": "garage, original"}, follow_redirects=False)
    original = _state()
    data = _export(c)

    # Mutate: delete a manual and add a new one.
    c.post(f"/manuals/{keep['id']}/delete", follow_redirects=False)
    added = _upload(c, tmp_path, f"Added-{name}", f"ADDED AFTER EXPORT {name}")
    assert _state() != original

    backups_before = set(get_settings().backups_dir.glob("pre-import-*"))
    r = _import(c, data)
    assert r.status_code == 200, r.text
    assert "Library replaced" in r.text
    assert _state() == original
    with db.get_conn() as conn:
        assert manuals_repo.get_by_sha(conn, added["sha256"]) is None
        assert manuals_repo.get(conn, keep["id"])["tags"] == "garage, original"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == library_io.schema_version()
    # The app still serves the restored manual and its file.
    assert c.get(f"/manuals/{keep['id']}/file").status_code == 200

    # The previous library was backed up before being replaced.
    new_backups = set(get_settings().backups_dir.glob("pre-import-*")) - backups_before
    assert len(new_backups) == 1
    last = new_backups.pop()
    assert (last / "files" / f"{added['sha256']}.pdf").exists()
    bconn = sqlite3.connect(last / "manualagerie.db")
    assert bconn.execute("SELECT count(*) FROM manuals WHERE sha256 = ?", (added["sha256"],)).fetchone()[0] == 1
    bconn.close()
    assert not list(get_settings().tmp_dir.glob("import-*"))


def test_confirmation_required(c, tmp_path):
    _upload(c, tmp_path, "Confirm.pdf", "CONFIRM")
    data = _export(c)
    before = _state()
    r = _import(c, data, confirm="yes")
    assert r.status_code == 400 and "REPLACE" in r.text
    assert _state() == before


@pytest.mark.parametrize("evil", ["../evil.txt", "files/../../evil.txt", "/etc/evil.txt", "files/..\\..\\evil.txt"])
def test_zip_slip_rejected(c, tmp_path, evil):
    _upload(c, tmp_path, f"Slip{abs(hash(evil))}.pdf", "SLIP")
    data = _rezip(_export(c), add={evil: b"pwned"})
    before = _state()
    r = _import(c, data)
    assert r.status_code == 400 and "unsafe path" in r.text
    assert _state() == before
    s = get_settings()
    assert not (s.data_dir.parent / "evil.txt").exists() and not (s.data_dir / "evil.txt").exists()


def test_corrupt_db_rejected_live_untouched(c, tmp_path):
    _upload(c, tmp_path, "Corrupt.pdf", "CORRUPT")
    data = _export(c)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        good = bytearray(zf.read("manualagerie.db"))
    # Keep the header so SQLite opens it, but smash the pages after the first.
    bad = bytes(good[:4096]) + b"\xde\xad\xbe\xef" * ((len(good) - 4096) // 4)
    before = _state()
    backups_before = set(get_settings().backups_dir.iterdir())
    for blob in (bad, b"this is not a database at all" * 100):
        r = _import(c, _rezip(data, replace={"manualagerie.db": blob}))
        assert r.status_code == 400 and "nothing was changed" in r.text, r.text
        assert _state() == before
    assert set(get_settings().backups_dir.iterdir()) == backups_before, "rejected imports must not touch backups"
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_newer_schema_rejected(c, tmp_path):
    _upload(c, tmp_path, "Newer.pdf", "NEWER")
    data = _export(c)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest["schema_version"] = library_io.schema_version() + 1
    before = _state()
    r = _import(c, _rezip(data, replace={"manifest.json": json.dumps(manifest)}))
    assert r.status_code == 400 and "newer version" in r.text
    assert _state() == before

    # A DB whose own user_version is newer is rejected too, even if the manifest claims otherwise.
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        snap = tmp_path / "newer.db"
        snap.write_bytes(zf.read("manualagerie.db"))
    conn = sqlite3.connect(snap)
    conn.execute(f"PRAGMA user_version = {library_io.schema_version() + 1}")
    conn.commit()
    conn.close()
    r = _import(c, _rezip(data, replace={"manualagerie.db": snap.read_bytes()}))
    assert r.status_code == 400
    assert _state() == before


def test_older_schema_is_migrated(c, tmp_path):
    _upload(c, tmp_path, "Older.pdf", "OLDER")
    data = _export(c)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        snap = tmp_path / "older.db"
        snap.write_bytes(zf.read("manualagerie.db"))
        manifest = json.loads(zf.read("manifest.json"))
    # A genuinely older database: only migrations 0001-0003 applied, so every later migration must replay on import.
    snap.unlink()
    conn = sqlite3.connect(snap)
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name.split("_", 1)[0]) <= 3:
            conn.executescript(f.read_text())
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    manifest["schema_version"] = 3
    r = _import(c, _rezip(data, replace={"manualagerie.db": snap.read_bytes(), "manifest.json": json.dumps(manifest)}))
    assert r.status_code == 200, r.text
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == library_io.schema_version()
        conn.execute("SELECT ocr_method FROM pages").fetchall()  # added by 0011
        conn.execute("SELECT count(*) FROM shares").fetchall()  # added by 0005


def test_missing_manifest_and_caps(c, tmp_path, monkeypatch):
    _upload(c, tmp_path, "Caps.pdf", "CAPS")
    data = _export(c)
    before = _state()
    r = _import(c, _rezip(data, drop={"manifest.json"}))
    assert r.status_code == 400 and "manifest.json is missing" in r.text
    r = _import(c, b"not a zip")
    assert r.status_code == 400 and "not a zip" in r.text
    r = _import(c, _rezip(data, add={"secret_key": b"x"}))
    assert r.status_code == 400 and "unexpected entry" in r.text

    s = get_settings()
    monkeypatch.setattr(s, "import_max_files", 2)
    r = _import(c, data)
    assert r.status_code == 400 and "entries" in r.text
    monkeypatch.setattr(s, "import_max_files", 20000)
    monkeypatch.setattr(s, "import_max_mb", 0)
    r = _import(c, data)
    assert r.status_code == 400 and "MB" in r.text
    assert _state() == before


def test_worker_pauses_and_resumes():
    worker.start()
    try:
        with worker.paused(timeout=10):
            assert worker._paused.is_set()
        for _ in range(50):
            if not worker._paused.is_set():
                break
            time.sleep(0.05)
        assert not worker._paused.is_set()
        assert worker._thread.is_alive()
    finally:
        worker.stop()


def test_different_page_size_is_imported(c, tmp_path):
    keep = _upload(c, tmp_path, "PageSize.pdf", "PAGE SIZE")
    data = _export(c)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        snap = tmp_path / "pagesize.db"
        snap.write_bytes(zf.read("manualagerie.db"))
    conn = sqlite3.connect(snap)
    conn.execute("PRAGMA page_size = 8192")
    conn.execute("VACUUM")
    conn.close()
    c.post(f"/manuals/{keep['id']}/delete", follow_redirects=False)
    r = _import(c, _rezip(data, replace={"manualagerie.db": snap.read_bytes()}))
    assert r.status_code == 200, r.text
    with db.get_conn() as conn:
        assert manuals_repo.get_by_sha(conn, keep["sha256"]) is not None


def test_failure_during_swap_puts_previous_library_back(c, tmp_path, monkeypatch):
    _upload(c, tmp_path, "Swap.pdf", "SWAP")
    data = _export(c)
    _upload(c, tmp_path, "SwapLater.pdf", "SWAP LATER")
    before = _state()
    real = library_io._restore_db
    calls = []

    def flaky(path):
        calls.append(path)
        real(path)
        if len(calls) == 1:  # the new DB lands, then something goes wrong
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(library_io, "_restore_db", flaky)
    r = _import(c, data)
    assert r.status_code == 500 and "previous library" in r.text
    assert len(calls) == 2
    assert _state() == before
