CREATE TABLE source_versions (
	id VARCHAR(128) NOT NULL,
	user_id VARCHAR(128) NOT NULL,
	source_id VARCHAR(128) NOT NULL,
	version INTEGER NOT NULL,
	deleted BOOLEAN DEFAULT false NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_source_versions PRIMARY KEY (id),
	CONSTRAINT uq_source_versions_user_id_source_id_version UNIQUE (user_id, source_id, version),
	CONSTRAINT source_history_version CHECK (version > 0),
	CONSTRAINT uq_source_versions_user_id_id UNIQUE (user_id, id),
	CONSTRAINT fk_source_versions_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);
INSERT INTO source_versions (id,user_id,source_id,version,deleted,payload,created_at)
SELECT gen_random_uuid()::text,user_id,id,version,false,
jsonb_build_object('kind',kind,'enabled',enabled,'config',payload),created_at FROM user_sources;
