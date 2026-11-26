from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def identifier():
    return Column("id", String(128), primary_key=True)


def owner(primary_key=False):
    return Column(
        "user_id", String(128), ForeignKey("users.id"), nullable=False, primary_key=primary_key
    )


def payload(name="payload"):
    return Column(name, JSONB, nullable=False, server_default=text("'{}'::jsonb"))


def created():
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


def owned(name, *columns):
    return Table(
        name,
        metadata,
        identifier(),
        owner(),
        *columns,
        UniqueConstraint("user_id", "id"),
        created(),
    )


users = Table(
    "users",
    metadata,
    identifier(),
    Column("login", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("password_hash", Text),
    Column("role", Text, nullable=False, server_default="user"),
    Column("disabled", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint(
        "length(btrim(login)) > 0 AND login = lower(btrim(login))", name="user_login_normalized"
    ),
    CheckConstraint("role IN ('admin','user')", name="user_role"),
    created(),
)
settings = Table(
    "user_settings",
    metadata,
    owner(primary_key=True),
    Column("key", Text, primary_key=True),
    payload("value"),
)
revisions = Table(
    "user_revisions",
    metadata,
    Column("user_id", String(128), ForeignKey("users.id"), primary_key=True),
    Column("revision", BigInteger, nullable=False, server_default="0"),
)
sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", Text, primary_key=True),
    owner(),
    Column("csrf", Text, nullable=False),
    Column("expires_at", Float, nullable=False),
)
login_attempts = Table(
    "login_attempts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    owner(),
    Column("ip", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index(
    "login_attempts_owner_ip_time",
    login_attempts.c.user_id,
    login_attempts.c.ip,
    login_attempts.c.created_at,
)
invitations = owned(
    "invitations",
    Column("created_by_id", String(128), ForeignKey("users.id")),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("expires_at", Float, nullable=False),
    Column("used_at", Float),
    Column("revoked_at", Float),
)
password_resets = owned(
    "password_reset_tokens",
    Column("created_by_id", String(128), ForeignKey("users.id")),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("expires_at", Float, nullable=False),
    Column("used_at", Float),
    Column("revoked_at", Float),
)
source_definitions = Table(
    "source_definitions", metadata, Column("kind", Text, primary_key=True), payload()
)
user_sources = owned(
    "user_sources",
    Column("kind", Text, ForeignKey("source_definitions.kind"), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("version", Integer, nullable=False, server_default="1"),
    payload(),
)
search_profiles = owned(
    "search_profiles",
    Column("version", Integer, nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    payload(),
    UniqueConstraint("user_id", "version"),
)
Index(
    "one_active_search_profile",
    search_profiles.c.user_id,
    unique=True,
    postgresql_where=search_profiles.c.active.is_(True),
)
jobs = Table(
    "jobs",
    metadata,
    identifier(),
    Column("source", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("company", Text, nullable=False),
    Column("location", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("published_at", Text),
    payload(),
    Column("content_hash", Text, nullable=False),
    created(),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "jsonb_typeof(payload) = 'object' AND NOT "
        "(payload ?| ARRAY['score','match_reasons','location_status','user_id',"
        "'applied_at','archived_at','documents','facts','template'])",
        name="job_content_is_public",
    ),
)
identities = Table(
    "job_identities",
    metadata,
    Column("source_key", Text, primary_key=True),
    Column("job_id", String(128), ForeignKey("jobs.id"), nullable=False),
)
Index("job_identities_job", identities.c.job_id)
matches = Table(
    "job_matches",
    metadata,
    Column("user_id", String(128), ForeignKey("users.id"), primary_key=True),
    Column("job_id", String(128), ForeignKey("jobs.id"), primary_key=True),
    Column("score", Integer, nullable=False),
    Column("reasons", JSONB, nullable=False),
    Column("location_status", Text, nullable=False),
    payload(),
    CheckConstraint("score BETWEEN 0 AND 100", name="match_score_range"),
)
states = Table(
    "user_jobs",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("job_id", String(128), primary_key=True),
    Column("applied_at", Text),
    Column("archived_at", Text),
    ForeignKeyConstraint(["user_id", "job_id"], ["job_matches.user_id", "job_matches.job_id"]),
)
source_fetches = Table(
    "source_fetches",
    metadata,
    identifier(),
    Column("cache_key", Text, nullable=False),
    payload(),
    created(),
)
source_fetch_jobs = Table(
    "source_fetch_jobs",
    metadata,
    Column("fetch_id", String(128), ForeignKey("source_fetches.id"), primary_key=True),
    Column("job_id", String(128), ForeignKey("jobs.id"), primary_key=True),
)
search_runs = owned(
    "search_runs",
    Column("profile_version", Text),
    payload(),
    Column("completed_at", DateTime(timezone=True)),
)
search_run_jobs = Table(
    "search_run_jobs",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("job_id", String(128), ForeignKey("jobs.id"), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("score", Integer, nullable=False),
    Column("reasons", JSONB, nullable=False),
    payload(),
    ForeignKeyConstraint(["user_id", "run_id"], ["search_runs.user_id", "search_runs.id"]),
    ForeignKeyConstraint(["user_id", "job_id"], ["job_matches.user_id", "job_matches.job_id"]),
    UniqueConstraint("user_id", "run_id", "position"),
)
snapshots = owned(
    "job_snapshots",
    Column("job_ids", JSONB, nullable=False),
    Column("snapshot_at", Float, nullable=False),
    payload(),
)
templates = owned(
    "candidate_templates",
    Column("status", Text, nullable=False),
    payload(),
    CheckConstraint("status IN ('draft','active','retired')", name="template_status"),
)
Index(
    "one_active_template",
    templates.c.user_id,
    unique=True,
    postgresql_where=templates.c.status == "active",
)
conversations = owned(
    "conversations",
    payload(),
    Column("scope", Text, nullable=False),
    Column("template_id", String(128)),
    Column("job_id", String(128)),
    UniqueConstraint("user_id", "scope"),
    ForeignKeyConstraint(
        ["user_id", "template_id"], ["candidate_templates.user_id", "candidate_templates.id"]
    ),
    ForeignKeyConstraint(["user_id", "job_id"], ["job_matches.user_id", "job_matches.job_id"]),
    CheckConstraint("(template_id IS NULL) <> (job_id IS NULL)", name="conversation_context"),
)
messages = owned(
    "messages",
    Column("conversation_id", String(128), nullable=False),
    Column("role", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("sequence", BigInteger, Identity(), nullable=False, unique=True),
    UniqueConstraint("user_id", "conversation_id", "request_id", "role"),
    ForeignKeyConstraint(
        ["user_id", "conversation_id"], ["conversations.user_id", "conversations.id"]
    ),
    CheckConstraint("role IN ('user','assistant')", name="message_role"),
)
tasks = owned(
    "tasks",
    Column("kind", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    payload(),
    payload("result"),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("error", Text),
    Column("job_id", String(128)),
    Column("template_id", String(128)),
    Column("conversation_id", String(128)),
    Column("lease_until", Float),
    Column("worker_id", Text),
    Column("attempts", Integer, nullable=False, server_default="0"),
    UniqueConstraint("user_id", "request_id"),
    ForeignKeyConstraint(["user_id", "job_id"], ["job_matches.user_id", "job_matches.job_id"]),
    ForeignKeyConstraint(
        ["user_id", "template_id"], ["candidate_templates.user_id", "candidate_templates.id"]
    ),
    ForeignKeyConstraint(
        ["user_id", "conversation_id"], ["conversations.user_id", "conversations.id"]
    ),
    CheckConstraint(
        "kind IN ('search','chat','cv','cover_letter','posting_check')", name="task_kind"
    ),
    CheckConstraint(
        "status IN ('queued','running','verifying','rendering','ready','failed',"
        "'needs_input','blocked','cancelled','deleted')",
        name="task_status",
    ),
    CheckConstraint("attempts >= 0", name="task_attempts"),
    CheckConstraint(
        "template_id IS NOT DISTINCT FROM (payload #>> '{context,template,id}')",
        name="task_template_payload",
    ),
    CheckConstraint(
        "job_id IS NOT DISTINCT FROM COALESCE(payload ->> 'job_id', "
        "payload #>> '{context,job,id}')",
        name="task_job_payload",
    ),
    CheckConstraint(
        "conversation_id IS NOT DISTINCT FROM (payload ->> 'conversation_id')",
        name="task_conversation_payload",
    ),
    CheckConstraint(
        "NOT (payload ? 'context') OR "
        "(payload #>> '{context,owner_id}') IS NOT DISTINCT FROM user_id",
        name="task_context_owner",
    ),
)
Index(
    "one_active_task_scope",
    tasks.c.user_id,
    tasks.c.scope,
    unique=True,
    postgresql_where=tasks.c.status.in_(["queued", "running", "verifying", "rendering"]),
)
Index("tasks_pending", tasks.c.status, tasks.c.created_at)
task_requests = Table(
    "task_requests",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("request_id", Text, primary_key=True),
    Column("task_id", String(128), nullable=False),
    payload(),
    ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"]),
)
artifacts = owned(
    "artifacts",
    Column("task_id", String(128), nullable=False),
    payload(),
    ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"]),
)
usage_events = owned(
    "usage_events",
    Column("task_id", String(128), nullable=False),
    Column("role", Text, nullable=False),
    Column("external_id", Text),
    payload(),
    ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"]),
)
usage_reservations = owned(
    "usage_reservations",
    Column("task_id", String(128), nullable=False),
    payload(),
    ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"]),
)
telegram_bindings = Table(
    "telegram_bindings",
    metadata,
    Column("chat_id", BigInteger, primary_key=True),
    owner(),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    created(),
    UniqueConstraint("user_id", "chat_id"),
)
deliveries = Table(
    "deliveries",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("job_id", String(128), ForeignKey("jobs.id"), primary_key=True),
    created(),
    ForeignKeyConstraint(
        ["user_id", "chat_id"], ["telegram_bindings.user_id", "telegram_bindings.chat_id"]
    ),
)
import_runs = Table(
    "import_runs",
    metadata,
    identifier(),
    Column("user_id", String(128), ForeignKey("users.id"), nullable=False),
    Column("source_hash", Text, nullable=False, unique=True),
    payload(),
    created(),
    UniqueConstraint("user_id", "id"),
)
legacy_records = owned(
    "legacy_records",
    Column("import_id", String(128), nullable=False),
    Column("kind", Text, nullable=False),
    Column("legacy_key", Text, nullable=False),
    Column("linked", Boolean, nullable=False, server_default=text("false")),
    payload(),
    UniqueConstraint("user_id", "kind", "legacy_key"),
    ForeignKeyConstraint(["user_id", "import_id"], ["import_runs.user_id", "import_runs.id"]),
)

auth_attempts = Table(
    "auth_attempts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("key_hash", Text, nullable=False),
    Column("ip_hash", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("auth_attempts_key_time", auth_attempts.c.key_hash, auth_attempts.c.created_at)
Index("auth_attempts_ip_time", auth_attempts.c.ip_hash, auth_attempts.c.created_at)

source_versions = owned(
    "source_versions",
    Column("source_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("deleted", Boolean, nullable=False, server_default=text("false")),
    payload(),
    UniqueConstraint("user_id", "source_id", "version"),
    CheckConstraint("version > 0", name="source_history_version"),
)

runtime_control = Table(
    "runtime_control", metadata, Column("key", Text, primary_key=True), payload()
)
search_schedules = Table(
    "search_schedules",
    metadata,
    owner(primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("interval_seconds", Integer, nullable=False, server_default="10800"),
    Column("next_run_at", Float, nullable=False),
    CheckConstraint("interval_seconds BETWEEN 900 AND 86400", name="schedule_interval"),
)
Index("usage_reservations_task", usage_reservations.c.user_id, usage_reservations.c.task_id)
Index(
    "usage_event_call",
    usage_events.c.external_id,
    unique=True,
    postgresql_where=text("external_id IS NOT NULL AND payload ? 'reservation_id'"),
)
