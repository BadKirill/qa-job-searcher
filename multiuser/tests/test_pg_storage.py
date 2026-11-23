import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pg_fixtures import personal_store, tamper_task
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from qa_job_bot import pg_schema as s
from qa_job_bot.models import Job
from qa_job_bot.pg_database import REVISION, Database
from qa_job_bot.pg_jobs import PublicJobRepository
from qa_job_bot.pg_queue import QueueRepository
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web_store import StoreError


def vacancy(index=0, **changes):
    return replace(
        Job(
            "Synthetic",
            str(index),
            "Senior QA Engineer",
            f"Company {index}",
            "Europe",
            f"https://example.com/pg/{index}",
            description="Confirmed software testing requirements.",
            remote=True,
            score=80,
            match_reasons=["роль: qa engineer"],
        ),
        **changes,
    )


def candidate(store):
    return store.add_template(
        {
            "facts": [
                {
                    "id": "f1",
                    "text": "Confirmed Python experience",
                    "source": "Synthetic confirmation",
                }
            ]
        }
    )


def document_task(store, request_id="document-1", scope="document", kind="cv"):
    job = vacancy()
    store.upsert_jobs([job])
    template = candidate(store)
    task = store.enqueue(
        kind,
        scope,
        {
            "job_id": job.fingerprint,
            "context": {"job": store.get_job(job.fingerprint), "template": template},
        },
        request_id,
    )
    return task, job, template


@pytest.mark.parametrize("url", ["sqlite://", "mysql://host/db", "postgresql+psycopg2://host/db"])
def test_database_rejects_other_drivers(url):
    with pytest.raises(ValueError, match="PostgreSQL"):
        Database(url)


@pytest.mark.parametrize("schema", ["", "a;drop schema public", "public,other", "A", "a" * 64])
def test_database_rejects_unsafe_schema_names(schema):
    with pytest.raises(ValueError, match="schema"):
        Database("postgresql://localhost/test", schema)


@pytest.mark.parametrize("owner", [None, "", " ", 123])
def test_personal_repository_requires_explicit_owner(pg_database, owner):
    with pytest.raises(ValueError, match="owner"):
        PostgresStore(pg_database, owner)


def test_migrations_are_transactional_repeatable_and_serialized(pg_database):
    assert pg_database.revision() == REVISION
    tables = set(inspect(pg_database.engine).get_table_names())
    assert set(s.metadata.tables) <= tables
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: pg_database.migrate(), range(3)))
    assert set(inspect(pg_database.engine).get_table_names()) == tables
    with pg_database.engine.begin() as connection:
        command.downgrade(pg_database.migration_config(connection), "base")
    with pg_database.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 0
    assert pg_database.revision() is None
    pg_database.migrate()
    assert pg_database.revision() == REVISION
    with pytest.raises(RuntimeError, match="rollback"), pg_database.transaction() as connection:
        connection.execute(
            s.users.insert().values(id="rolled-back", login="rollback", display_name="Rollback")
        )
        raise RuntimeError("rollback")
    with pg_database.engine.connect() as connection:
        assert connection.execute(select(s.users)).first() is None


def test_frozen_migration_matches_runtime_schema(pg_database):
    with pg_database.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, s.metadata) == []


def test_database_env_configuration_and_unmigrated_failure(monkeypatch, pg_database):
    monkeypatch.setenv("DATABASE_URL", pg_database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DATABASE_SCHEMA", f"qa_test_{uuid4().hex}")
    other = Database.from_env()
    try:
        assert other.revision() is None
        with pytest.raises(RuntimeError, match="migrations"):
            PostgresStore(other, "missing").initialize()
    finally:
        other.close()
    with pytest.raises(StoreError) as error:
        PostgresStore(pg_database, "missing").initialize()
    assert error.value.status == 403


def test_account_normalization_and_unique_login(pg_database):
    first = pg_database.create_user("  ＡLICE  ", " Alice ", role="admin")
    assert first["login"] == "alice" and first["display_name"] == "Alice"
    assert first["role"] == "admin"
    with pytest.raises(IntegrityError):
        pg_database.create_user("ALICE", "Other")
    for login, name, role in [(" ", "Name", "user"), ("b", "", "user"), ("c", "C", "root")]:
        with pytest.raises(StoreError) as error:
            pg_database.create_user(login, name, role=role)
        assert error.value.status == 422


def test_shared_job_content_has_independent_scores_states_and_revisions(pg_store):
    a = pg_store
    b = personal_store(a.database, "bob")
    job_a = vacancy(published_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert a.upsert_jobs([job_a]) == {job_a.fingerprint}
    assert b.list_jobs("active", 1)["total"] == 0
    with pytest.raises(StoreError) as error:
        b.get_job(job_a.fingerprint)
    assert error.value.status == 404
    job_b = replace(job_a, score=25, match_reasons=["another preference"])
    assert b.upsert_jobs([job_b]) == set()
    assert a.get_job(job_a.fingerprint)["score"] == 80
    assert b.get_job(job_a.fingerprint)["score"] == 25
    a.change_state(job_a.fingerprint, {"applied": True, "archived": True})
    assert b.get_job(job_a.fingerprint)["applied_at"] is None
    assert b.get_setting("revision", 0) == 0
    a.change_state(job_a.fingerprint, {"archived": False})
    assert a.list_jobs("applied", 1)["total"] == 1
    assert a.list_jobs("archive", 1)["total"] == 0
    b.upsert_jobs([replace(job_b, description="New shared public description", published_at=None)])
    assert a.get_job(job_a.fingerprint)["description"] == job_a.description
    assert a.get_job(job_a.fingerprint)["match_reasons"] == job_a.match_reasons
    with a.database.transaction() as connection:
        public = connection.execute(select(s.jobs)).mappings().one()
        assert public["published_at"] == "2026-11-01T00:00:00+00:00"
        assert not {"score", "match_reasons", "location_status"} & public["payload"].keys()


def test_public_ingestion_does_not_grant_personal_access(pg_store):
    jobs = [vacancy(), vacancy(1)]
    catalog = PublicJobRepository(pg_store.database)
    assert catalog.upsert_jobs([]) == set()
    catalog.upsert_jobs(jobs)
    assert pg_store.list_jobs("active", 1)["items"] == []
    for job in jobs:
        with pytest.raises(StoreError):
            pg_store.get_job(job.fingerprint)
    pg_store.resolve_identities(jobs)
    pg_store.upsert_jobs(jobs)
    assert pg_store.list_jobs("active", 1)["total"] == 2


def test_identity_collisions_aliases_and_concurrent_ingestion(pg_store):
    a = vacancy()
    b = replace(a, source_id="second", url="https://example.com/another")
    c = replace(a, source="Alias", source_id="alias")
    pg_store.upsert_jobs([a, b, c])
    assert a.fingerprint != b.fingerprint and a.fingerprint == c.fingerprint
    changed = replace(a, title="Changed title", location="Worldwide", resolved_fingerprint=None)
    pg_store.upsert_jobs([changed])
    assert changed.fingerprint == a.fingerprint
    other = personal_store(pg_store.database, "concurrent")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda store: store.upsert_jobs([vacancy(5)]), [pg_store, other])
        )
    assert sum(len(result) for result in results) == 1


@pytest.mark.parametrize("count", [0, 1, 9, 10, 11, 20, 21])
def test_personal_pagination_and_search_history(pg_store, count):
    jobs = [vacancy(index, score=100 - index) for index in range(count)]
    pg_store.upsert_jobs(jobs)
    pg_store.publish_search(jobs, [])
    first = pg_store.list_jobs("active", 1)
    assert first["total"] == count
    assert first["pages"] == max(1, (count + 9) // 10)
    combined = []
    for page in range(1, first["pages"] + 1):
        combined += pg_store.list_jobs("active", page, first["snapshot_id"])["items"]
    assert [row["id"] for row in combined] == [job.fingerprint for job in jobs]
    assert pg_store.list_jobs("active", 999, first["snapshot_id"])["page"] == first["pages"]
    with pg_store.database.transaction() as connection:
        assert len(connection.execute(select(s.search_run_jobs)).all()) == count
        assert connection.execute(select(s.search_runs)).mappings().one()["completed_at"]


def test_failed_source_retention_imports_expiry_and_foreign_snapshots(pg_store):
    a, b = vacancy(), vacancy(1, source="Second")
    pg_store.upsert_jobs([a, b])
    pg_store.publish_search([a, b], [])
    first = pg_store.list_jobs("active", 1)
    pg_store.publish_search([], ["Synthetic: TimeoutError"])
    assert pg_store.get_setting("current_jobs") == [a.fingerprint]
    assert pg_store.list_jobs("active", 1)["total"] == 1
    pg_store.set_setting("imported_jobs", [b.fingerprint])
    assert pg_store.list_jobs("active", 1)["total"] == 2
    other = personal_store(pg_store.database, "snapshot-other")
    with pytest.raises(StoreError) as error:
        other.list_jobs("active", 1, first["snapshot_id"])
    assert error.value.status == 404
    with pg_store.database.transaction() as connection:
        connection.execute(
            update(s.snapshots)
            .where(s.snapshots.c.id == first["snapshot_id"])
            .values(snapshot_at=0)
        )
    pg_store.list_jobs("active", 1)
    with pytest.raises(StoreError):
        pg_store.list_jobs("active", 1, first["snapshot_id"])
    for tab, page in [("wrong", 1), ("active", 0)]:
        with pytest.raises(StoreError) as error:
            pg_store.list_jobs(tab, page)
        assert error.value.status == 422
    for values in ({}, {"unknown": True}):
        with pytest.raises(StoreError) as error:
            pg_store.change_state(a.fingerprint, values)
        assert error.value.status == 422


def test_templates_and_conversations_are_personal_and_versioned(pg_store):
    a = pg_store
    b = personal_store(a.database, "template-other")
    first, second = candidate(a), candidate(b)
    a.activate_template(first["id"], None)
    b.activate_template(second["id"], None)
    replacement = candidate(a)
    a.update_template(
        replacement["id"], {"instructions": "new", "id": "forged", "status": "active"}
    )
    assert a.template(replacement["id"])["id"] == replacement["id"]
    with pytest.raises(StoreError) as error:
        a.activate_template(replacement["id"], None)
    assert error.value.status == 409
    a.activate_template(replacement["id"], first["id"])
    assert a.template(first["id"])["status"] == "retired"
    assert b.profile()["active"]["id"] == second["id"]
    assert a.conversation(template_id=replacement["id"])["id"] == replacement["conversation_id"]
    with pytest.raises(StoreError):
        a.update_template(first["id"], {"instructions": "invalid"})
    empty = a.add_template({"facts": []})
    with pytest.raises(StoreError):
        a.activate_template(empty["id"], replacement["id"])
    for operation in [
        lambda: b.template(first["id"]),
        lambda: b.get_conversation(first["conversation_id"]),
        lambda: b.messages(first["conversation_id"]),
        lambda: b.conversation(template_id=first["id"]),
    ]:
        with pytest.raises(StoreError) as error:
            operation()
        assert error.value.status == 404
    for args in ({}, {"job_id": "x", "template_id": "y"}):
        with pytest.raises(StoreError) as error:
            a.conversation(**args)
        assert error.value.status == 422


def test_messages_chat_idempotency_and_history_limits(pg_store):
    task, job, template = document_task(pg_store)
    pg_store.cancel_task(task["id"])
    conversation = pg_store.conversation(job_id=job.fingerprint)
    assert pg_store.get_conversation(conversation["id"]) == conversation
    for index in range(20):
        role = "assistant" if index % 2 else "user"
        message = pg_store.add_message(conversation["id"], role, f"message-{index}", str(index))
    assert pg_store.add_message(conversation["id"], role, "message-19", "19")["id"] == message["id"]
    with pytest.raises(StoreError):
        pg_store.add_message(conversation["id"], role, "different", "19")
    with pytest.raises(StoreError):
        pg_store.add_message(conversation["id"], "system", "unsafe", "system")
    assert len(pg_store.owner_messages(job.fingerprint)) == 8
    assert len(pg_store.document_conversation(job.fingerprint)) == 16
    assert len(pg_store.messages(conversation["id"])) == 20
    chat = pg_store.enqueue_message(conversation["id"], {"template": template}, "question", "chat")
    assert (
        pg_store.enqueue_message(conversation["id"], {"template": template}, "question", "chat")[
            "id"
        ]
        == chat["id"]
    )
    with pytest.raises(StoreError):
        pg_store.enqueue_message(conversation["id"], {"template": template}, "other", "other")
    pg_store.claim("worker")
    assert not pg_store.finish_chat(chat["id"], "wrong", "answer")
    assert pg_store.finish_chat(chat["id"], "worker", "answer")
    assert not pg_store.finish_chat(chat["id"], "worker", "duplicate")
    assert pg_store.messages(conversation["id"])[-1]["text"] == "answer"


def test_personal_request_ids_context_links_and_immutable_inputs(pg_store):
    a = pg_store
    b = personal_store(a.database, "tasks-other")
    task, job, template = document_task(a)
    other, _, _ = document_task(b)
    assert task["id"] != other["id"]
    context = {"job_id": job.fingerprint, "context": {"template": template}}
    request = {"operation": "document"}
    one = a.enqueue("cv", "same", context, "same-1", request)
    context["context"]["template"]["facts"][0]["text"] = "mutated caller"
    assert (
        a.task(one["id"], internal=True)["payload"]["context"]["template"]["facts"][0]["text"]
        != "mutated caller"
    )
    assert a.enqueue("cv", "same", {}, "same-1", request)["id"] == one["id"]
    assert a.enqueue("cv", "same", {}, "same-2")["id"] == one["id"]
    assert a.request_task("same-1", request)["id"] == one["id"]
    assert a.request_task("missing", {}) is None
    with pytest.raises(StoreError):
        a.request_task("same-1", {"operation": "different"})
    for kind, scope, key in [
        ("invalid", "scope", "key"),
        ("search", "", "key"),
        ("search", "scope", ""),
    ]:
        with pytest.raises(StoreError):
            a.enqueue(kind, scope, {}, key)
    with pytest.raises(StoreError):
        a.enqueue("search", "same", {}, "wrong-kind")
    with pytest.raises(StoreError) as error:
        b.enqueue("cv", "foreign", {"context": {"template": template}}, "foreign")
    assert error.value.status == 404
    with pytest.raises(StoreError):
        b.enqueue("search", "owner", {"context": {"owner_id": a.user_id}}, "owner")
    for operation in [lambda: b.task(one["id"]), lambda: b.cancel_task(one["id"])]:
        with pytest.raises(StoreError) as error:
            operation()
        assert error.value.status == 404


def test_queue_claims_once_across_workers_and_separates_internal_access(pg_store):
    stores = [pg_store, personal_store(pg_store.database, "queue-other")]
    for index, store in enumerate(stores):
        for number in range(4):
            store.enqueue("search", f"{number}", {}, f"{index}-{number}")
    queue = QueueRepository(pg_store.database)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(lambda index: queue.claim(f"worker-{index}"), range(8)))
    assert len({task["id"] for task in claimed}) == 8
    assert {task["user_id"] for task in claimed} == {store.user_id for store in stores}
    assert queue.claim("empty") is None
    for task in claimed:
        store = next(store for store in stores if store.user_id == task["user_id"])
        assert not store.finish_task(task["id"], "wrong", "ready")
        assert store.finish_task(task["id"], task["worker_id"], "ready")
        assert not store.finish_task(task["id"], task["worker_id"], "ready")


def test_expired_cancelled_and_disabled_tasks_cannot_publish(pg_store):
    task = pg_store.enqueue("search", "expired", {}, "expired")
    pg_store.claim("worker", lease_seconds=-1)
    assert not pg_store.finish_task(task["id"], "worker", "ready")
    assert pg_store.claim("next") is None
    assert pg_store.task(task["id"])["status"] == "failed"
    cancelled = pg_store.enqueue("search", "cancelled", {}, "cancelled")
    pg_store.claim("worker")
    pg_store.cancel_task(cancelled["id"])
    assert not pg_store.finish_task(cancelled["id"], "worker", "ready")
    with pytest.raises(StoreError):
        pg_store.finish_task(cancelled["id"], "worker", "arbitrary")
    blocked = pg_store.enqueue("search", "disabled", {}, "disabled")
    with pg_store.database.transaction() as connection:
        connection.execute(
            update(s.users).where(s.users.c.id == pg_store.user_id).values(disabled=True)
        )
    assert QueueRepository(pg_store.database).claim("disabled") is None
    with pytest.raises(StoreError) as error:
        pg_store.task(blocked["id"])
    assert error.value.status == 403


def test_artifacts_deletion_and_unverified_results(pg_store):
    task, job, _ = document_task(pg_store)
    with pytest.raises(StoreError):
        pg_store.document_task_for_deletion(task["id"])
    pg_store.claim("worker")
    artifact = pg_store.add_artifact(
        task["id"], {"filename": "synthetic.pdf", "format": "pdf", "path": "/synthetic/file.pdf"}
    )
    with pytest.raises(StoreError):
        pg_store.artifact(artifact["id"])
    pg_store.finish_task(
        task["id"],
        "worker",
        "ready",
        result={"verified": True, "approved_draft": {"title": "Synthetic"}},
    )
    assert pg_store.artifact(artifact["id"]) == artifact
    assert "approved_draft" not in pg_store.task(task["id"])["result"]
    assert pg_store.tasks(job.fingerprint)[0]["preview_available"]
    pg_store.delete_document_task(task["id"])
    assert pg_store.tasks() == []
    assert pg_store.task(task["id"], internal=True)["status"] == "deleted"
    for operation in [
        lambda: pg_store.artifact(artifact["id"]),
        lambda: pg_store.document_task_for_deletion(task["id"]),
    ]:
        with pytest.raises(StoreError) as error:
            operation()
        assert error.value.status == 404
    search = pg_store.enqueue("search", "search", {}, "search")
    pg_store.cancel_task(search["id"])
    with pytest.raises(StoreError) as error:
        pg_store.document_task_for_deletion(search["id"])
    assert error.value.status == 422


@pytest.mark.parametrize(
    "field", ["score", "match_reasons", "location_status", "user_id", "facts", "template"]
)
def test_database_rejects_private_fields_in_public_jobs(pg_store, field):
    pg_store.upsert_jobs([vacancy()])
    with pytest.raises(IntegrityError) as error, pg_store.database.transaction() as connection:
        connection.execute(update(s.jobs).values(payload={field: "private"}))
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "entity",
    [
        "conversation",
        "message",
        "task",
        "artifact",
        "request",
        "usage",
        "reservation",
        "state",
        "run",
    ],
)
def test_database_foreign_keys_reject_cross_owner_links(pg_store, entity):
    a = pg_store
    b = personal_store(a.database, "foreign-link")
    task, job, template = document_task(a)
    conversation = a.conversation(template_id=template["id"])
    b.upsert_jobs([replace(job)])
    values = {"id": str(uuid4()), "user_id": b.user_id}
    if entity == "conversation":
        table = s.conversations
        values.update(scope="foreign", template_id=template["id"])
    elif entity == "message":
        table = s.messages
        values.update(
            conversation_id=conversation["id"], role="user", text="foreign", request_id="f"
        )
    elif entity == "task":
        table = s.tasks
        values.update(
            kind="cv",
            scope="foreign",
            request_id="foreign",
            template_id=template["id"],
            payload={"context": {"owner_id": b.user_id, "template": {"id": template["id"]}}},
        )
    elif entity in {"artifact", "usage", "reservation"}:
        table = {
            "artifact": s.artifacts,
            "usage": s.usage_events,
            "reservation": s.usage_reservations,
        }[entity]
        values["task_id"] = task["id"]
        if entity == "usage":
            values["role"] = "generator"
    elif entity == "request":
        table = s.task_requests
        values.pop("id")
        values.update(task_id=task["id"], request_id="foreign")
    elif entity == "state":
        table = s.states
        values.pop("id")
        other = vacancy(500)
        a.upsert_jobs([other])
        values["job_id"] = other.fingerprint
    else:
        table = s.search_run_jobs
        a.publish_search([job], [])
        values.pop("id")
        values.update(
            run_id=a.get_setting("current_search_run"),
            job_id=job.fingerprint,
            position=0,
            score=50,
            reasons=[],
        )
    with pytest.raises(IntegrityError) as error, a.database.transaction() as connection:
        connection.execute(table.insert().values(**values))
    assert error.value.orig.sqlstate == "23503"


def test_database_enforces_unique_active_versions_and_payload_links(pg_store):
    first = candidate(pg_store)
    pg_store.activate_template(first["id"], None)
    second = candidate(pg_store)
    with pytest.raises(IntegrityError) as error, pg_store.database.transaction() as connection:
        connection.execute(
            update(s.templates).where(s.templates.c.id == second["id"]).values(status="active")
        )
    assert error.value.orig.sqlstate == "23505"
    task, _, _ = document_task(pg_store)
    for payload in [{}, {"context": {"owner_id": "foreign"}}]:
        with pytest.raises(IntegrityError) as error:
            tamper_task(pg_store, task["id"], payload=payload)
        assert error.value.orig.sqlstate == "23514"


def test_settings_and_sessions_remain_personal(pg_store):
    a = pg_store
    b = personal_store(a.database, "settings-other")
    a.set_setting("preferences", {"value": "A"})
    b.set_setting("preferences", {"value": "B"})
    assert a.get_setting("preferences") == {"value": "A"}
    assert b.get_setting("preferences") == {"value": "B"}
    for key in ("password", "revision"):
        with pytest.raises(StoreError):
            a.set_setting(key, "forged")
    a.set_initial_password("synthetic-hash")
    with pytest.raises(StoreError):
        a.set_initial_password("replacement")
    assert b.get_setting("password") is None
    now = time.time()
    a.save_session("hashed", "csrf", now, now + 10)
    assert a.find_session("hashed", now)["user_id"] == a.user_id
    assert b.find_session("hashed", now) is None
    assert a.find_session("hashed", now + 10) is None
    b.delete_session("hashed")
    assert a.find_session("hashed", now)
    a.save_session("next", "csrf", now + 11, now + 20)
    assert a.find_session("hashed", now) is None
    a.delete_session("next")
    for _ in range(10):
        a.record_login_attempt("same-ip", now)
    with pytest.raises(StoreError) as error:
        a.record_login_attempt("same-ip", now)
    assert error.value.status == 429
    b.record_login_attempt("same-ip", now)
    a.record_login_attempt("same-ip", now + 901)
    a.clear_login_attempts("same-ip")
    a.record_login_attempt("same-ip", now + 901)


def test_parallel_activation_and_idempotency_use_real_row_locks(pg_store):
    drafts = [candidate(pg_store), candidate(pg_store)]

    def activate(draft):
        try:
            return pg_store.activate_template(draft["id"], None)["id"]
        except StoreError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, drafts))
    assert sum(result is not None for result in results) == 1
    assert pg_store.profile()["active"]["id"] in results
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = list(
            executor.map(
                lambda _: pg_store.enqueue("search", "same-scope", {}, "same-key"), range(4)
            )
        )
    assert len({task["id"] for task in tasks}) == 1


def test_database_requires_owners_and_one_active_search_profile(pg_store):
    with pytest.raises(IntegrityError) as error, pg_store.database.transaction() as connection:
        connection.execute(s.templates.insert().values(id="unowned", status="draft"))
    assert error.value.orig.sqlstate == "23502"
    with pg_store.database.transaction() as connection:
        connection.execute(
            s.search_profiles.insert().values(id="profile-1", user_id=pg_store.user_id, version=1)
        )
    with pytest.raises(IntegrityError) as error, pg_store.database.transaction() as connection:
        connection.execute(
            s.search_profiles.insert().values(id="profile-2", user_id=pg_store.user_id, version=2)
        )
    assert error.value.orig.sqlstate == "23505"
