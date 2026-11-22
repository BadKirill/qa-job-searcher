CREATE TABLE runtime_control (
    key TEXT PRIMARY KEY,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE search_schedules (
    user_id VARCHAR(128) PRIMARY KEY REFERENCES users(id),
    enabled BOOLEAN NOT NULL DEFAULT true,
    interval_seconds INTEGER NOT NULL DEFAULT 10800,
    next_run_at DOUBLE PRECISION NOT NULL,
    CONSTRAINT schedule_interval CHECK (interval_seconds BETWEEN 900 AND 86400)
);
CREATE INDEX usage_reservations_task ON usage_reservations(user_id, task_id);
CREATE UNIQUE INDEX usage_event_call ON usage_events(external_id)
WHERE external_id IS NOT NULL AND payload ? 'reservation_id';
