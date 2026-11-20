CREATE TABLE manuals (
  id            INTEGER PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,
  filename      TEXT NOT NULL,
  file_ext      TEXT NOT NULL DEFAULT 'pdf',
  title         TEXT,
  device_name   TEXT,
  brand         TEXT,
  model_numbers TEXT,
  category      TEXT,
  doc_type      TEXT,
  room          TEXT,
  summary       TEXT,
  notes         TEXT,
  page_count    INTEGER,
  pages_done    INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'queued',
  error         TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE pages (
  manual_id  INTEGER NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
  page_no    INTEGER NOT NULL,
  text       TEXT NOT NULL,
  lang       TEXT,
  ocr        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (manual_id, page_no)
);

CREATE TABLE chunks (
  id         INTEGER PRIMARY KEY,
  manual_id  INTEGER NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
  page_start INTEGER NOT NULL,
  page_end   INTEGER NOT NULL,
  heading    TEXT,
  lang       TEXT,
  text       TEXT NOT NULL
);
CREATE INDEX chunks_manual ON chunks(manual_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  heading, text,
  content='chunks', content_rowid='id',
  tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, heading, text) VALUES (new.id, coalesce(new.heading, ''), new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, heading, text) VALUES ('delete', old.id, coalesce(old.heading, ''), old.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, heading, text) VALUES ('delete', old.id, coalesce(old.heading, ''), old.text);
  INSERT INTO chunks_fts(rowid, heading, text) VALUES (new.id, coalesce(new.heading, ''), new.text);
END;

CREATE TABLE jobs (
  id         INTEGER PRIMARY KEY,
  manual_id  INTEGER NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'queued',
  attempts   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX jobs_status ON jobs(status, id);

CREATE TABLE conversations (
  id         INTEGER PRIMARY KEY,
  title      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,
  content         TEXT,
  tool_calls      TEXT,
  tool_call_id    TEXT,
  citations       TEXT,
  model           TEXT,
  usage           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX messages_conv ON messages(conversation_id, id);

CREATE TABLE login_attempts (
  ip         TEXT NOT NULL,
  at         REAL NOT NULL
);
