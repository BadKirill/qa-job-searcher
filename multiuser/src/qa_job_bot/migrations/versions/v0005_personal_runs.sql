ALTER TABLE job_matches ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE search_run_jobs ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE job_snapshots ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb;
UPDATE job_matches AS m SET payload = jsonb_build_object('job', j.payload,
 'fetched_at', j.last_seen_at, 'location_status', m.location_status, 'freshness', 'legacy')
FROM jobs AS j WHERE j.id = m.job_id;
UPDATE search_run_jobs AS r SET payload = m.payload FROM job_matches AS m
WHERE r.user_id = m.user_id AND r.job_id = m.job_id;
UPDATE job_snapshots AS snap SET payload = jsonb_build_object('cards', COALESCE((
 SELECT jsonb_object_agg(m.job_id, m.payload || jsonb_build_object('score',m.score,'reasons',m.reasons))
 FROM job_matches AS m WHERE m.user_id = snap.user_id AND snap.job_ids ? m.job_id
), '{}'::jsonb));
