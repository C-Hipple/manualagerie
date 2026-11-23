"""Semantic search: local embeddings (fastembed, BAAI/bge-small-en-v1.5) stored in sqlite-vec.

Everything here is optional. If SEMANTIC_SEARCH is off, fastembed or the model can't be loaded,
or sqlite-vec isn't present, callers get None / [] and search stays keyword-only.

The embedder is swappable (`set_embedder`) so tests can use a deterministic fake.
"""
import logging
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from .. import db
from ..config import get_settings

log = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
BATCH_SIZE = 32
MAX_EMBED_CHARS = 2000  # the model truncates at 512 tokens anyway


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[Sequence[float]]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class FastEmbedder:
    """fastembed's ONNX runtime on CPU. bge uses a query prefix, which query_embed adds."""

    dim = DIM

    def __init__(self, cache_dir: Path, local_files_only: bool = False):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(MODEL_NAME, cache_dir=str(cache_dir), local_files_only=local_files_only)

    def embed_documents(self, texts: list[str]) -> list[Sequence[float]]:
        return list(self._model.passage_embed(texts, batch_size=BATCH_SIZE))

    def embed_query(self, text: str) -> Sequence[float]:
        return next(iter(self._model.query_embed(text)))


# ---- embedder lifecycle ----

_lock = threading.Lock()
_loaded = threading.Event()  # set once a load attempt has finished (or an embedder was injected)
_embedder: Embedder | None = None
_status = "not loaded"
_loader: threading.Thread | None = None


def enabled() -> bool:
    return get_settings().semantic_search


def set_embedder(embedder: Embedder | None, status: str | None = None) -> None:
    """Install an embedder (tests, or a custom backend). None marks semantic search unavailable."""
    global _embedder, _status
    with _lock:
        _embedder = embedder
        _status = status or ("ready (injected)" if embedder else "unavailable")
        _loaded.set()


def reset() -> None:
    """Forget any loaded/injected embedder so the next call loads again (tests)."""
    global _embedder, _status, _loader
    with _lock:
        _embedder, _status, _loader = None, "not loaded", None
        _loaded.clear()


def status() -> str:
    if not enabled():
        return "off (SEMANTIC_SEARCH=false)"
    return _status


def _load() -> Embedder | None:
    s = get_settings()
    try:
        import fastembed  # noqa: F401
    except ImportError:
        log.warning("fastembed is not installed: semantic search falls back to keyword-only")
        return None
    # A cache baked into the image first, then the persistent volume, and only then the network.
    attempts = []
    if s.embed_bundled_dir:
        attempts.append((Path(s.embed_bundled_dir), True))
    attempts += [(s.models_dir, True), (s.models_dir, False)]
    last = None
    for cache_dir, offline in attempts:
        if offline and not cache_dir.is_dir():
            continue
        try:
            emb = FastEmbedder(cache_dir, local_files_only=offline)
            log.info("embedding model %s loaded from %s%s", MODEL_NAME, cache_dir, "" if offline else " (downloaded)")
            return emb
        except Exception as e:  # missing files, no network, broken onnxruntime, ...
            last = e
    log.warning("embedding model %s unavailable (%s: %s): semantic search falls back to keyword-only",
                MODEL_NAME, type(last).__name__, last)
    return None


def load_embedder() -> Embedder | None:
    """Blocking: load the model once (or return what is already there)."""
    global _embedder, _status
    if not enabled():
        return None
    with _lock:
        if _loaded.is_set():
            return _embedder
        _status = "loading"
        emb = _load()
        _embedder = emb
        _status = "ready" if emb else "unavailable (see startup log)"
        _loaded.set()
        return emb


def current_embedder() -> Embedder | None:
    """Non-blocking: the embedder if it has finished loading, else None (search must never wait)."""
    if not enabled() or not _loaded.is_set():
        return None
    return _embedder


def start_background_load() -> None:
    """Load the model off the request path, then backfill any chunks that lack vectors."""
    global _loader
    if not enabled() or _loaded.is_set() or (_loader and _loader.is_alive()):
        return

    def run():
        try:
            if load_embedder() is None:
                return
            conn = db.connect()
            try:
                if table_ready(conn):
                    n = enqueue_backfill(conn)
                    conn.commit()
                    if n:
                        log.info("queued embedding backfill for %d manual(s)", n)
                        from ..ingest import worker
                        worker.wake()
            finally:
                conn.close()
        except Exception:
            log.exception("semantic search warm-up failed; keyword search is unaffected")

    _loader = threading.Thread(target=run, name="embedder-loader", daemon=True)
    _loader.start()


# ---- storage ----

def table_ready(conn: sqlite3.Connection) -> bool:
    if not db.has_vec(conn):
        return False
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec'").fetchone() is not None


def available(conn: sqlite3.Connection) -> bool:
    """Should new chunks be embedded? (The model may still be loading.)"""
    return enabled() and table_ready(conn)


def _blob(vec: Sequence[float]) -> bytes:
    import sqlite_vec

    return sqlite_vec.serialize_float32([float(x) for x in vec])


def delete_manual(conn: sqlite3.Connection, manual_id: int) -> None:
    """Drop a manual's vectors. Call before its chunks are deleted (delete / re-index)."""
    if not table_ready(conn):
        return  # nothing stored, or the extension is gone: the startup orphan sweep cleans up later
    conn.execute("DELETE FROM chunks_vec WHERE manual_id = ?", (int(manual_id),))


def delete_orphans(conn: sqlite3.Connection) -> int:
    if not table_ready(conn):
        return 0
    # A vector is stale if its chunk is gone, or the chunk id was reused by another manual.
    orphans = [r[0] for r in conn.execute(
        "SELECT v.chunk_id FROM chunks_vec v LEFT JOIN chunks c ON c.id = v.chunk_id AND c.manual_id = v.manual_id "
        "WHERE c.id IS NULL"
    )]
    conn.executemany("DELETE FROM chunks_vec WHERE chunk_id = ?", [(i,) for i in orphans])
    return len(orphans)


def manuals_missing_vectors(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT c.manual_id FROM chunks c JOIN manuals m ON m.id = c.manual_id "
        "WHERE m.status = 'ready' AND NOT EXISTS (SELECT 1 FROM chunks_vec v WHERE v.chunk_id = c.id) "
        "ORDER BY c.manual_id"
    ).fetchall()
    return [r[0] for r in rows]


def enqueue_backfill(conn: sqlite3.Connection) -> int:
    """Queue one `embed` job per ready manual that has chunks without vectors."""
    from ..repo import jobs as jobs_repo

    if not available(conn):
        return 0
    delete_orphans(conn)
    pending = {r[0] for r in conn.execute(
        "SELECT manual_id FROM jobs WHERE status IN ('queued', 'running') AND kind IN ('embed', 'ingest', 'reindex')")}
    n = 0
    for manual_id in manuals_missing_vectors(conn):
        if manual_id not in pending:
            jobs_repo.enqueue(conn, manual_id, "embed")
            n += 1
    return n


def embed_manual(conn: sqlite3.Connection, manual_id: int, embedder: Embedder | None = None,
                 batch_size: int = BATCH_SIZE) -> int:
    """Embed every chunk of a manual that has no vector yet, committing per batch. Returns count."""
    embedder = embedder or load_embedder()
    if embedder is None or not table_ready(conn):
        log.info("manual %s: semantic search unavailable, skipping embeddings", manual_id)
        return 0
    done = 0
    while True:
        rows = conn.execute(
            "SELECT c.id, c.heading, c.text FROM chunks c WHERE c.manual_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM chunks_vec v WHERE v.chunk_id = c.id) ORDER BY c.id LIMIT ?",
            (int(manual_id), batch_size),
        ).fetchall()
        if not rows:
            break
        texts = [(f"{r['heading']}\n{r['text']}" if r["heading"] else r["text"])[:MAX_EMBED_CHARS] for r in rows]
        vecs = embedder.embed_documents(texts)
        if len(vecs) != len(rows):
            raise RuntimeError(f"embedder returned {len(vecs)} vectors for {len(rows)} texts")
        conn.executemany(
            "INSERT INTO chunks_vec (chunk_id, manual_id, embedding) VALUES (?, ?, ?)",
            [(r["id"], int(manual_id), _blob(v)) for r, v in zip(rows, vecs)],
        )
        conn.commit()
        done += len(rows)
    if done:
        log.info("manual %s: embedded %d chunks", manual_id, done)
    return done


# ---- query ----

def search(conn: sqlite3.Connection, query: str, manual_ids: list[int] | None = None, limit: int = 30,
           preferred_langs: list[str] | None = None) -> list[dict] | None:
    """Nearest chunks to the query, best first. None means semantic search is unavailable."""
    embedder = current_embedder()
    if embedder is None or not query.strip():
        return None
    try:
        if not table_ready(conn):
            return None
        qvec = _blob(embedder.embed_query(query))
        params: list = [qvec, int(limit) * 2]
        where = ""
        if manual_ids:
            where = f" AND manual_id IN ({','.join('?' * len(manual_ids))})"
            params.extend(int(x) for x in manual_ids)
        knn = conn.execute(
            f"SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?{where} ORDER BY distance",
            params,
        ).fetchall()
    except Exception as e:
        log.warning("vector search failed, using keyword results only: %s: %s", type(e).__name__, e)
        return None
    if not knn:
        return []
    dist = {r["chunk_id"]: r["distance"] for r in knn}
    rows = conn.execute(
        f"""SELECT c.id AS chunk_id, c.manual_id, c.page_start, c.page_end, c.heading, c.lang, c.text,
                   m.title AS manual_title, m.device_name, m.tags
            FROM chunks c JOIN manuals m ON m.id = c.manual_id
            WHERE m.status = 'ready' AND c.id IN ({','.join('?' * len(dist))})""",
        list(dist),
    ).fetchall()
    out = [dict(r) | {"distance": dist[r["chunk_id"]]} for r in rows]
    prefs = set(preferred_langs or [])
    # Same language rule as FTS: other-language chunks rank after preferred ones.
    out.sort(key=lambda r: (bool(prefs and r["lang"] and r["lang"] not in prefs), r["distance"]))
    return out[:limit]
