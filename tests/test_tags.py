import pytest

from app.llm.prompts import SYSTEM_PROMPT, build_catalog, catalog_line
from app.repo import manuals as manuals_repo


@pytest.fixture(autouse=True)
def _clean_manuals(conn):
    """The test database is shared across modules; leave no manuals behind."""
    yield
    conn.execute("DELETE FROM manuals")
    conn.commit()


def _manual(conn, sha, filename, **fields):
    mid = manuals_repo.create(conn, sha, filename, "pdf")
    manuals_repo.update_fields(conn, mid, {"status": "ready", **fields})
    conn.commit()
    return manuals_repo.get(conn, mid)


@pytest.mark.parametrize("raw, expected", [
    ("downstairs AC, shed", ["downstairs AC", "shed"]),
    ("  Downstairs AC ;; downstairs ac ,,, shed ", ["Downstairs AC", "shed"]),
    ("kitchen  closet", ["kitchen closet"]),
    ("", []),
    (None, []),
    (["a", "b", "a"], ["a", "b"]),
])
def test_parse_tags(raw, expected):
    assert manuals_repo.parse_tags(raw) == expected


def test_update_fields_normalizes_tags(conn):
    mid = manuals_repo.create(conn, "sha-tags-1", "ac.pdf", "pdf")
    manuals_repo.update_fields(conn, mid, {"tags": " downstairs AC ,, Downstairs ac; shed "})
    conn.commit()
    assert manuals_repo.get(conn, mid)["tags"] == "downstairs AC, shed"
    manuals_repo.update_fields(conn, mid, {"tags": ""})
    conn.commit()
    assert manuals_repo.get(conn, mid)["tags"] is None


def test_catalog_line_shows_tags(conn):
    m = _manual(conn, "sha-tags-2", "ac-down.pdf", device_name="Air conditioner",
                tags="downstairs AC, near the back door", summary="Portable unit.")
    line = catalog_line(m)
    assert "#" in line and "(tags: downstairs AC, near the back door)" in line


def test_tag_matches_rank_above_summary_text(conn):
    _manual(conn, "sha-tags-3", "ac-up.pdf", device_name="Air conditioner",
            tags="upstairs AC", summary="Portable air conditioner for the bedroom.")
    tagged = _manual(conn, "sha-tags-4", "ac-down.pdf", device_name="Air conditioner",
                     tags="downstairs AC", summary="Window unit, R32 refrigerant.")
    rows = manuals_repo.search_catalog(conn, "downstairs AC")
    assert [r["id"] for r in rows][0] == tagged["id"]
    # A location word only present in a tag still finds the manual.
    assert tagged["id"] in [r["id"] for r in manuals_repo.search_catalog(conn, "downstairs")]


def test_system_prompt_and_catalog_use_tags(conn):
    m = _manual(conn, "sha-tags-5", "ac-down.pdf", device_name="Air conditioner",
                tags="downstairs AC")
    catalog, _ = build_catalog([m], 10)
    assert "downstairs AC" in catalog
    assert "tags" in SYSTEM_PROMPT.lower()


def test_migration_adds_tags_to_an_existing_library(tmp_path):
    from app import db
    old = db.connect(tmp_path / "old.db")
    old.executescript((db.MIGRATIONS_DIR / "0001_init.sql").read_text())
    old.execute("PRAGMA user_version = 1")
    old.execute("INSERT INTO manuals (sha256, filename) VALUES ('abc', 'old.pdf')")
    old.commit()
    assert db.migrate(old) >= 2
    row = old.execute("SELECT filename, tags FROM manuals").fetchone()
    assert row["filename"] == "old.pdf" and row["tags"] is None
    old.close()


def test_fts_hits_carry_tags(conn, pdf_path):
    from app.search import fts
    from tests.test_ingest import _ingest
    mid = _ingest(conn, pdf_path)
    manuals_repo.update_fields(conn, mid, {"tags": "downstairs AC"})
    conn.commit()
    hits = fts.search(conn, "drain hose", [mid], 3, ["en"])
    assert hits and hits[0]["tags"] == "downstairs AC"
