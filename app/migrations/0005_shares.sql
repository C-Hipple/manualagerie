-- Public, read-only share pages made from an answer. The body and citations
-- are a snapshot: editing or deleting the source conversation later doesn't
-- change what the link shows. /s/<token> is the only route reachable without
-- the household password; a revoked share 404s like an unknown token.
CREATE TABLE shares (
  id              INTEGER PRIMARY KEY,
  token           TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  body            TEXT NOT NULL,
  citations       TEXT,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  revoked_at      TEXT
);
