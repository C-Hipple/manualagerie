-- Personal tags/aliases the user would call a device by, e.g. "downstairs AC".
-- The assistant uses these to tell similar manuals apart.
ALTER TABLE manuals ADD COLUMN tags TEXT;
