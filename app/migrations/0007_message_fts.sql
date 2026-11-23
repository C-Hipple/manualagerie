-- Full-text index over searchable chat text: user messages and final assistant
-- text. Tool results and assistant rows carrying tool_calls are not indexed.
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content,
  content='messages', content_rowid='id',
  tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages
WHEN new.role IN ('user', 'assistant') AND new.tool_calls IS NULL AND coalesce(new.content, '') <> ''
BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages
WHEN old.role IN ('user', 'assistant') AND old.tool_calls IS NULL AND coalesce(old.content, '') <> ''
BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER messages_fts_au_del AFTER UPDATE ON messages
WHEN old.role IN ('user', 'assistant') AND old.tool_calls IS NULL AND coalesce(old.content, '') <> ''
BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER messages_fts_au_ins AFTER UPDATE ON messages
WHEN new.role IN ('user', 'assistant') AND new.tool_calls IS NULL AND coalesce(new.content, '') <> ''
BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

INSERT INTO messages_fts(rowid, content)
SELECT id, content FROM messages
WHERE role IN ('user', 'assistant') AND tool_calls IS NULL AND coalesce(content, '') <> '';
