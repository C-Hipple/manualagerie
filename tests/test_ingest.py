import hashlib
import shutil

from app.config import get_settings
from app.ingest.chunk import chunk_pages
from app.ingest.extract import extract_pages
from app.ingest.lang import detect_lang
from app.ingest.worker import process_manual
from app.repo import manuals as manuals_repo
from app.search import fts


def test_extract_detects_headings_and_pages(pdf_path):
    pages = extract_pages(pdf_path)
    assert [p.page_no for p in pages] == [1, 2, 3, 4, 5]
    assert "3/8 inch compression fitting" in pages[1].text
    headings = [para.text for p in pages for para in p.paragraphs if para.is_heading]
    assert "WATER SUPPLY CONNECTION" in headings
    assert "DRAIN HOSE" in headings
    assert pages[4].text == ""


def test_chunks_keep_page_ranges_and_headings(pdf_path):
    pages = extract_pages(pdf_path)
    for p in pages:
        p.lang = detect_lang(p.text)
    chunks = chunk_pages(pages)
    assert chunks
    for c in chunks:
        assert c["page_end"] - c["page_start"] <= 1
    drain = [c for c in chunks if "5/8 inch" in c["text"]]
    assert drain and drain[0]["page_start"] == 3
    assert drain[0]["heading"] == "DRAIN HOSE"


def test_language_detection(pdf_path):
    pages = extract_pages(pdf_path)
    assert detect_lang(pages[3].text) == "fr"
    assert detect_lang(pages[2].text) == "en"


def _ingest(conn, path):
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = manuals_repo.get_by_sha(conn, sha)
    if existing:
        return existing["id"]
    shutil.copy(path, get_settings().files_dir / f"{sha}.pdf")
    mid = manuals_repo.create(conn, sha, path.name, "pdf")
    conn.commit()
    process_manual(conn, mid)
    return mid


def test_process_and_search(conn, pdf_path):
    mid = _ingest(conn, pdf_path)
    m = manuals_repo.get(conn, mid)
    assert m["status"] == "ready"
    assert m["page_count"] == 5
    hits = fts.search(conn, "What size is the drain hose connection?", [mid], 5, ["en"])
    assert hits and hits[0]["page_start"] == 3
    hits = fts.search(conn, "3/8 compression", None, 5, ["en"])
    assert hits[0]["page_start"] <= 2 <= hits[0]["page_end"]
    # porter stemming: "connections" -> "connect"
    assert fts.search(conn, "connecting", [mid], 5)


def test_preferred_language_ranks_first(conn, pdf_path):
    mid = _ingest(conn, pdf_path)
    hits = fts.search(conn, "instructions installer installing dishwasher", [mid], 5, ["en"])
    assert hits[0]["lang"] == "en"


def test_match_query_sanitizes():
    assert fts.build_match_query('what is "the" hose* size? 3/4"') == '"hose" OR "size" OR "3/4"'
    assert fts.build_match_query("the of and") is None
    assert fts.build_match_query("NEAR(a b) OR x") == '"NEAR" OR "b" OR "x"'
