ALTER TABLE import_runs ADD CONSTRAINT uq_import_runs_user_id_id UNIQUE (user_id, id);

CREATE TABLE legacy_records (
	id VARCHAR(128) NOT NULL,
	user_id VARCHAR(128) NOT NULL,
	import_id VARCHAR(128) NOT NULL,
	kind TEXT NOT NULL,
	legacy_key TEXT NOT NULL,
	linked BOOLEAN DEFAULT false NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_legacy_records PRIMARY KEY (id),
	CONSTRAINT uq_legacy_records_user_id_kind_legacy_key UNIQUE (user_id, kind, legacy_key),
	CONSTRAINT fk_legacy_records_user_id_import_id_import_runs FOREIGN KEY(user_id, import_id) REFERENCES import_runs (user_id, id),
	CONSTRAINT uq_legacy_records_user_id_id UNIQUE (user_id, id),
	CONSTRAINT fk_legacy_records_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);
