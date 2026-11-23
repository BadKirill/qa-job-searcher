ALTER TABLE conversations ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb;
