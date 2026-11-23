"""Semantic search: RRF, embed jobs, backfill, cleanup and FTS-only fallback.

No network and no real model: a deterministic hashed bag-of-words embedder stands in for
bge-small. A tiny synonym table lets it "understand" a paraphrase that shares no keyword
with the manual text, which is exactly the case FTS can't handle.
"""
import hashlib
import math
import re

import pytest

from app import db
from app.config import get_settings
from app.ingest import worker
from app.llm import tools
from app.repo import jobs as jobs_repo
from app.repo import manuals as manuals_repo
from app.search import hybrid, vector
from tests.conftest import login

SYNONYMS = {"tubing": "hose", "outlet": "drain", "width": "diameter", "girth": "diameter",
            "faucet": "water", "plumbing": "supply"}


class FakeEmbedder:
    dim = vector.DIM

    def __init__(self):
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z]+", text.lower()):
            w = SYNONYMS.get(w, w)
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        self.calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


needs_vec = pytest.mark.skipif(not db.has_vec(db.connect(":memory:")), reason="sqlite-vec wheel not available")

CHUNKS = [
    ("DRAIN HOSE", "The drain hose fits 5/8 inch to 1 inch diameter connections at the sink drain."),
    ("WATER SUPPLY", "Connect the water inlet using a 3/8 inch compression fitting on the supply line."),
    ("RACKS", "Adjust the upper rack height by lifting the rack and pressing the levers."),
]


@pytest.fixture
def vconn(tmp_path):
    c = db.connect(tmp_path / "v.db")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def fake():
    emb = FakeEmbedder()
    vector.set_embedder(emb)
    yield emb
    vector.reset()


@pytest.fixture
def semantic(monkeypatch):
    def set_flag(on: bool):
        monkeypatch.setattr(get_settings(), "semantic_search", on)
    set_flag(True)
    return set_flag


def add_manual(conn, sha="a" * 64, chunks=CHUNKS) -> int:
    mid = manuals_repo.create(conn, sha, "dishwasher.pdf", "pdf")
    manuals_repo.replace_content(
        conn, mid, [{"page_no": i + 1, "text": t, "lang": "en"} for i, (_, t) in enumerate(chunks)],
        [{"page_start": i + 1, "page_end": i + 1, "heading": h, "lang": "en", "text": t}
         for i, (h, t) in enumerate(chunks)],
    )
    manuals_repo.update_fields(conn, mid, {"status": "ready"})
    conn.commit()
    return mid


def vec_count(conn, manual_id=None) -> int:
    if manual_id is None:
        return conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
    return conn.execute("SELECT count(*) FROM chunks_vec WHERE manual_id = ?", (manual_id,)).fetchone()[0]


# ---- RRF ----

def test_rrf_merge_order():
    fused = hybrid.rrf_merge({"fts": [1, 2, 3], "vector": [3, 4, 1]}, k=60)
    ids = [i for i, _, _ in fused]
    # 1: 1/61 + 1/63 ; 3: 1/63 + 1/61 (tie, broken by best single rank: both 1 -> earlier source fts)
    assert ids == [1, 3, 2, 4]
    s = dict((i, sc) for i, sc, _ in fused)
    assert s[1] == pytest.approx(1 / 61 + 1 / 63)
    assert s[2] == pytest.approx(1 / 62) and s[4] == pytest.approx(1 / 62)
    assert dict((i, r) for i, _, r in fused)[3] == {"fts": 3, "vector": 1}


def test_rrf_both_sources_beat_single_top_hit():
    fused = hybrid.rrf_merge({"fts": [10, 20], "vector": [30, 20]})
    assert [i for i, _, _ in fused][0] == 20


# ---- hybrid search with the fake embedder ----

@needs_vec
def test_paraphrase_found_only_by_vector(vconn, fake, semantic):
    mid = add_manual(vconn)
    assert vector.embed_manual(vconn, mid) == len(CHUNKS)
    query = "outlet tubing width"  # no keyword in common with any chunk
    assert hybrid.fts.search(vconn, query, None, 5, ["en"]) == []
    hits = hybrid.search(vconn, query, None, 5, ["en"])
    assert hits and hits[0]["heading"] == "DRAIN HOSE"
    assert hits[0]["sources"] == ["vector"]
    assert hits[0]["snippet"].startswith("The drain hose")


@needs_vec
def test_hybrid_marks_both_sources_and_respects_manual_filter(vconn, fake, semantic):
    a = add_manual(vconn)
    b = add_manual(vconn, sha="b" * 64)
    vector.embed_manual(vconn, a)
    vector.embed_manual(vconn, b)
    hits = hybrid.search(vconn, "drain hose diameter", [b], 5, ["en"])
    assert hits and all(h["manual_id"] == b for h in hits)
    assert hits[0]["heading"] == "DRAIN HOSE" and hits[0]["sources"] == ["fts", "vector"]
    assert "«" in hits[0]["snippet"]  # FTS highlight kept
    # the search_manuals tool goes through the same path
    out = tools.search_manuals(vconn, "outlet tubing width", [b])
    assert f"[#{b} p.1]" in out and "drain hose" in out


@needs_vec
def test_embed_batches(vconn, fake, semantic):
    chunks = [(f"STEP {i}", f"Step number {i} of the procedure.") for i in range(7)]
    mid = add_manual(vconn, chunks=chunks)
    assert vector.embed_manual(vconn, mid, batch_size=3) == 7
    assert fake.calls == 3
    assert vector.embed_manual(vconn, mid) == 0  # nothing left to do


# ---- jobs & backfill ----

@needs_vec
def test_backfill_enqueues_and_embed_job_fills_vectors(vconn, fake, semantic):
    a = add_manual(vconn)
    b = add_manual(vconn, sha="b" * 64)
    vector.embed_manual(vconn, a)
    assert vector.manuals_missing_vectors(vconn) == [b]
    assert vector.enqueue_backfill(vconn) == 1
    assert vector.enqueue_backfill(vconn) == 0  # already queued: no duplicate
    job = jobs_repo.claim_next(vconn)
    assert job["kind"] == "embed" and job["manual_id"] == b
    worker.run_job(vconn, job)
    assert vec_count(vconn, b) == len(CHUNKS)
    assert vector.manuals_missing_vectors(vconn) == []


@needs_vec
def test_process_manual_queues_embed_job(conn, pdf_path, semantic):
    # uses the shared test DB and the real ingest path
    from tests.test_ingest import _ingest
    mid = _ingest(conn, pdf_path)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE manual_id = ?", (mid,))]
    assert "embed" in kinds


# ---- cleanup ----

@needs_vec
def test_delete_and_reindex_remove_vectors(vconn, fake, semantic):
    a = add_manual(vconn)
    b = add_manual(vconn, sha="b" * 64)
    vector.embed_manual(vconn, a)
    vector.embed_manual(vconn, b)
    manuals_repo.delete(vconn, a)
    vconn.commit()
    assert vec_count(vconn, a) == 0 and vec_count(vconn, b) == len(CHUNKS)
    # re-index replaces chunks: old vectors go with them
    manuals_repo.replace_content(vconn, b, [], [{"page_start": 1, "page_end": 1, "text": "new text"}])
    assert vec_count(vconn, b) == 0


@needs_vec
def test_orphan_sweep(vconn, fake, semantic):
    a = add_manual(vconn)
    vector.embed_manual(vconn, a)
    vconn.execute("DELETE FROM manuals WHERE id = ?", (a,))  # bypasses the repo, as a cascade would
    assert vec_count(vconn) == len(CHUNKS)
    assert vector.delete_orphans(vconn) == len(CHUNKS)
    assert vec_count(vconn) == 0


# ---- graceful degradation ----

def test_fallback_when_disabled(vconn, fake, semantic):
    semantic(False)
    mid = add_manual(vconn)
    assert vector.embed_manual(vconn, mid) == 0
    assert hybrid.search(vconn, "outlet tubing width", None, 5, ["en"]) == []
    hits = hybrid.search(vconn, "drain hose", None, 5, ["en"])
    assert hits and hits[0]["sources"] == ["fts"] and "rrf" not in hits[0]
    assert vector.status().startswith("off")


def test_fallback_when_model_unavailable(vconn, semantic, monkeypatch):
    vector.reset()
    monkeypatch.setattr(vector, "_load", lambda: None)  # e.g. no network to download the model
    try:
        mid = add_manual(vconn)
        assert vector.load_embedder() is None
        assert vector.embed_manual(vconn, mid) == 0  # embed job is a no-op, not a failure
        hits = hybrid.search(vconn, "drain hose", None, 5, ["en"])
        assert hits and hits[0]["sources"] == ["fts"]
    finally:
        vector.reset()


def test_fallback_when_fastembed_missing(semantic, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("fastembed"):
            raise ImportError("no fastembed")
        return real_import(name, *a, **kw)

    vector.reset()
    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        assert vector.load_embedder() is None
        assert vector.status().startswith("unavailable")
    finally:
        vector.reset()


def test_fallback_when_sqlite_vec_missing(tmp_path, fake, semantic, monkeypatch):
    monkeypatch.setattr(db, "sqlite_vec", None)
    c = db.connect(tmp_path / "novec.db")
    try:
        assert not db.has_vec(c)
        version = db.migrate(c)  # vec migration is skipped, the rest still applies
        assert version == max(int(f.name.split("_")[0]) for f in db.MIGRATIONS_DIR.glob("*.sql"))
        assert not vector.table_ready(c)
        mid = add_manual(c)
        assert vector.enqueue_backfill(c) == 0
        hits = hybrid.search(c, "drain hose", None, 5, ["en"])
        assert hits and hits[0]["sources"] == ["fts"]
        manuals_repo.delete(c, mid)  # cleanup is a no-op without the table
    finally:
        c.close()


@needs_vec
def test_vec_table_created_later_when_extension_appears(tmp_path, monkeypatch):
    path = tmp_path / "late.db"
    with monkeypatch.context() as m:
        m.setattr(db, "sqlite_vec", None)
        c = db.connect(path)
        db.migrate(c)
        c.close()
    c = db.connect(path)
    try:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec'").fetchone()
        db.migrate(c)
        assert vector.table_ready(c)
    finally:
        c.close()


@needs_vec
def test_db_with_vec_table_still_works_without_extension(vconn, tmp_path, fake, semantic, monkeypatch):
    mid = add_manual(vconn)
    vector.embed_manual(vconn, mid)
    path = vconn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(db, "sqlite_vec", None)
    c = db.connect(path)
    try:
        db.migrate(c)
        hits = hybrid.search(c, "drain hose", None, 5, ["en"])
        assert hits and hits[0]["sources"] == ["fts"]
        manuals_repo.delete(c, mid)
        c.commit()
    finally:
        c.close()


@needs_vec
def test_search_page_shows_sources(conn, pdf_path, fake, semantic):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_ingest import _ingest
    mid = _ingest(conn, pdf_path)
    vector.embed_manual(conn, mid)
    with TestClient(app) as client:
        login(client)
        r = client.get("/search", params={"q": "outlet tubing width", "manual_id": mid})
        assert r.status_code == 200
        assert "Semantic search: ready" in r.text
        assert "source-vector" in r.text and "semantic #1" in r.text
        r = client.get("/search", params={"q": "drain hose", "manual_id": mid})
        assert "source-fts" in r.text and "source-vector" in r.text
