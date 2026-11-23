-- Recurring household maintenance (change the furnace filter every 90 days, ...).
CREATE TABLE maintenance_tasks (
  id            INTEGER PRIMARY KEY,
  title         TEXT NOT NULL,
  manual_id     INTEGER REFERENCES manuals(id) ON DELETE SET NULL,
  interval_days INTEGER NOT NULL CHECK (interval_days > 0),
  last_done     TEXT,                       -- ISO date, NULL until first done
  notes         TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_maintenance_manual ON maintenance_tasks(manual_id);

-- History: one row per time a task was done.
CREATE TABLE maintenance_log (
  id      INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES maintenance_tasks(id) ON DELETE CASCADE,
  done_on TEXT NOT NULL,                    -- ISO date
  note    TEXT
);
CREATE INDEX idx_maintenance_log_task ON maintenance_log(task_id, done_on);
