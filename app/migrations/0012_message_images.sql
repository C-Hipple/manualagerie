-- Screenshots/photos attached to a user's chat message. The files live under
-- {DATA_DIR}/chat_images/<filename>; app/repo/chat.py removes them when the
-- conversation is deleted (the rows go with the ON DELETE CASCADE).
CREATE TABLE message_images (
  id              INTEGER PRIMARY KEY,
  message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  filename        TEXT NOT NULL UNIQUE,
  mime            TEXT NOT NULL,
  width           INTEGER NOT NULL,
  height          INTEGER NOT NULL,
  bytes           INTEGER NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX message_images_msg ON message_images(message_id, id);
CREATE INDEX message_images_conv ON message_images(conversation_id);
