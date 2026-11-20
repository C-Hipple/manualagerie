-- Household notes: facts worth remembering that don't live in any manual
-- ("Upstairs AC filters: 16x25x1, two of them"). The chat agent sees them
-- alongside the manual catalog and cites them as [note #N].
CREATE TABLE notes (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  body       TEXT,
  tags       TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
