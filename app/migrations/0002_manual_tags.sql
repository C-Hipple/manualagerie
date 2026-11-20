-- Free-form tags the household types in ("downstairs AC", "shed mower", "kitchen").
-- They are the user's own words for a device, so the chat agent can tell two
-- similar manuals apart when the question names a location or a nickname.
ALTER TABLE manuals ADD COLUMN tags TEXT;
