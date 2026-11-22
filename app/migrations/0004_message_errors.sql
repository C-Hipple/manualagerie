-- Failed chat turns are saved as assistant rows flagged as errors, so they still show after a reload
-- (instead of the conversation silently ending on the user's message). They're never sent back to the LLM.
ALTER TABLE messages ADD COLUMN error INTEGER NOT NULL DEFAULT 0;
