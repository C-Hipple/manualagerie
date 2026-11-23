-- Per-user accounts within one household. The library, notes, maintenance and
-- shares stay shared; accounts record who asked what and who did what.
-- Existing rows keep NULL in the new columns and show as "unknown".
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT NOT NULL,
  password_hash TEXT NOT NULL,              -- scrypt$n$r$p$salt$hash (base64)
  is_admin      INTEGER NOT NULL DEFAULT 0,
  session_gen   INTEGER NOT NULL DEFAULT 0, -- bumped on password reset/disable: old cookies stop working
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  disabled_at   TEXT
);

ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX idx_conversations_user ON conversations(user_id, updated_at);

ALTER TABLE shares ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE notes ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE maintenance_log ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL;

-- Failed logins are now limited per IP and per username.
ALTER TABLE login_attempts ADD COLUMN username TEXT;
CREATE INDEX idx_login_attempts_username ON login_attempts(username, at);
