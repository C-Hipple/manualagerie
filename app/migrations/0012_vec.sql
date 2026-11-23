-- requires: sqlite-vec
-- Phase 4 semantic search: one 384-dim embedding (BAAI/bge-small-en-v1.5) per chunk.
-- vec0 tables can't take foreign keys, so app/search/vector.py deletes a manual's rows
-- before its chunks go (delete / re-index) and sweeps orphans on startup.
-- Kept idempotent: it is re-run on startup whenever sqlite-vec is loaded, so a database
-- migrated without the extension gets the table as soon as the extension is available.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
  chunk_id  INTEGER PRIMARY KEY,
  manual_id INTEGER,
  embedding float[384] distance_metric=cosine
);
