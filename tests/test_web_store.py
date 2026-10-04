import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from qa_job_bot.models import Job
from qa_job_bot.web_store import StoreError, WebStore, digest, encode


@pytest.fixture
def store(tmp_path: Path) -> WebStore:
    instance = WebStore(tmp_path / "database.sqlite3")
    instance.initialize()
    return instance


def make_job(index: int = 0, source: str = "Source") -> Job:
    return Job(
        source,
        str(index),
        "QA Engineer",
        f"Company {index}",
        "Europe",
        f"https://jobs.example/{index}",
        description="Requirements\nEnglish B2\nPython",
        score=100 - index,
    )


def add_jobs(store: WebStore, count: int) -> list[Job]:
    jobs = [make_job(index) for index in range(count)]
    store.upsert_jobs(jobs)
    store.publish_search(jobs, [])
    return jobs


def draft(store: WebStore, text: str = "Confirmed Python experience") -> dict:
    return store.add_template({"facts": [{"id": "fact-1", "text": text}], "filename": "cv.docx"})


def assert_status(error, expected: int) -> None:
    assert error.value.status == expected


def test_canonical_encoding_and_content_digest() -> None:
    assert encode({"b": 2, "a": "текст"}) == '{"a":"текст","b":2}'
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": "1"})
    assert StoreError("invalid").status == 400


def test_initialize_and_settings_preserve_values_after_restart(store: WebStore) -> None:
    assert store.get_setting("missing", "default") == "default"
    store.set_setting("options", {"language": "en", "ids": [1, 2]})
    before = store.get_setting("revision")
    restarted = WebStore(store.path)
    restarted.initialize()
    assert restarted.get_setting("options") == {"language": "en", "ids": [1, 2]}
    restarted.set_setting("options", {"language": "ru"})
    assert restarted.get_setting("revision") == before + 1
    with restarted._connect() as db:
        assert [
            row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [1, 2]


@pytest.mark.parametrize("count", [0, 1, 9, 10, 11, 20, 21])
def test_pagination_boundaries_and_stable_order(store: WebStore, count: int) -> None:
    jobs = add_jobs(store, count)
    first = store.list_jobs("active", 1)
    assert first["total"] == count
    assert first["pages"] == max(1, (count + 9) // 10)
    assert [entry["id"] for entry in first["items"]] == [job.fingerprint for job in jobs[:10]]
    combined = []
    for page in range(1, first["pages"] + 1):
        result = store.list_jobs("active", page, first["snapshot_id"])
        assert result["page"] == page
        combined.extend(entry["id"] for entry in result["items"])
    assert combined == [job.fingerprint for job in jobs]
    last = store.list_jobs("active", 9999, first["snapshot_id"])
    assert last["page"] == first["pages"]
    assert len(set(combined)) == count


@pytest.mark.parametrize("tab,page", [("other", 1), ("active", 0), ("archive", -1)])
def test_pagination_rejects_invalid_requests(store: WebStore, tab: str, page: int) -> None:
    with pytest.raises(StoreError) as error:
        store.list_jobs(tab, page)
    assert_status(error, 422)


def test_invalid_and_expired_snapshot_handling(store: WebStore, monkeypatch) -> None:
    with pytest.raises(StoreError) as error:
        store.list_jobs("active", 1, "unknown")
    assert_status(error, 409)
    with store._connect() as db:
        db.execute("INSERT INTO web_snapshots VALUES ('expired','[]',0)")
    store.list_jobs("active", 1)
    with pytest.raises(StoreError) as error:
        store.list_jobs("active", 1, "expired")
    assert_status(error, 409)


def test_snapshot_stays_stable_during_new_search_and_archive_preserves_state(
    store: WebStore,
) -> None:
    jobs = add_jobs(store, 11)
    snapshot = store.list_jobs("active", 1)["snapshot_id"]
    last_id = jobs[-1].fingerprint
    marked = store.change_state(last_id, {"applied": True, "archived": True})
    assert marked["applied_at"] and marked["archived_at"]
    second = store.list_jobs("active", 2, snapshot)
    assert second["page"] == 1
    assert second["total"] == 10
    newest = make_job(999)
    store.upsert_jobs([newest])
    store.publish_search([newest], [])
    assert store.list_jobs("active", 1)["total"] == 1
    assert store.list_jobs("active", 1, snapshot)["total"] == 10
    archive = store.list_jobs("archive", 1)
    assert [item["id"] for item in archive["items"]] == [last_id]
    restored = store.change_state(last_id, {"archived": False})
    assert restored["archived_at"] is None
    assert restored["applied_at"] == marked["applied_at"]
    assert last_id in {item["id"] for item in store.list_jobs("active", 1)["items"]}
    cleared = store.change_state(last_id, {"applied": False})
    assert cleared["applied_at"] is None
    store.upsert_jobs([replace(jobs[-1], description="Changed description")])
    assert store.get_job(last_id)["applied_at"] is None


def test_listing_before_first_search_and_update_keeps_decisions(store: WebStore) -> None:
    job = make_job()
    store.upsert_jobs([job])
    assert store.list_jobs("active", 1)["total"] == 1
    original = store.change_state(job.fingerprint, {"applied": True, "archived": True})
    store.upsert_jobs([replace(job, description="New body", salary="EUR 4000")])
    restarted = WebStore(store.path)
    restarted.initialize()
    updated = restarted.get_job(job.fingerprint)
    assert updated["description"] == "New body"
    assert updated["salary"] == "EUR 4000"
    assert updated["applied_at"] == original["applied_at"]
    assert updated["archived_at"] == original["archived_at"]


@pytest.mark.parametrize("values", [{}, {"ready": True}, {"applied": True, "unknown": False}])
def test_invalid_job_state_is_rejected_without_changes(store: WebStore, values: dict) -> None:
    job = add_jobs(store, 1)[0]
    with pytest.raises(StoreError) as error:
        store.change_state(job.fingerprint, values)
    assert_status(error, 422)
    assert store.get_job(job.fingerprint)["applied_at"] is None


def test_missing_and_legacy_jobs_are_unavailable(store: WebStore) -> None:
    with pytest.raises(StoreError) as error:
        store.get_job("missing")
    assert_status(error, 404)
    job = add_jobs(store, 1)[0]
    with store._connect() as db:
        db.execute("UPDATE jobs SET payload=NULL")
    with pytest.raises(StoreError) as error:
        store.get_job(job.fingerprint)
    assert_status(error, 404)
    assert store.list_jobs("active", 1)["items"] == []


def test_failed_sources_preserve_previous_jobs_only_for_matching_source(store: WebStore) -> None:
    retained = make_job(1, "Failed: board")
    removed = make_job(2, "Successful")
    fresh = make_job(3, "Successful")
    store.upsert_jobs([retained, removed, fresh])
    store.publish_search([retained, removed], [])
    store.set_setting("current_jobs", [retained.fingerprint, removed.fingerprint, "missing"])
    store.publish_search([fresh], ["Failed: board: ReadTimeout"])
    assert store.get_setting("current_jobs") == [fresh.fingerprint, retained.fingerprint]
    assert store.get_setting("search_errors") == ["Failed: board: ReadTimeout"]
    assert store.get_setting("last_search_at")
    store.publish_search([retained], ["Failed: board: ReadTimeout"])
    assert store.get_setting("current_jobs") == [retained.fingerprint]


def test_imported_jobs_stay_visible_outside_current_source_window(store: WebStore) -> None:
    jobs = add_jobs(store, 2)
    snapshot = store.list_jobs("active", 1)["snapshot_id"]
    store.publish_search([], [])
    store.set_setting("imported_jobs", [jobs[0].fingerprint])
    fresh = store.list_jobs("active", 1)
    assert [job["id"] for job in fresh["items"]] == [jobs[0].fingerprint]
    assert store.list_jobs("active", 1, snapshot)["total"] == 2
    store.change_state(jobs[0].fingerprint, {"archived": True})
    assert store.list_jobs("active", 1)["total"] == 0
    assert store.list_jobs("archive", 1)["items"][0]["id"] == jobs[0].fingerprint
    store.publish_search([], [])
    store.change_state(jobs[0].fingerprint, {"archived": False})
    assert store.list_jobs("active", 1)["items"][0]["id"] == jobs[0].fingerprint


def test_template_draft_activation_replacement_and_conflicts(store: WebStore) -> None:
    assert store.profile() == {"active": None, "drafts": []}
    first = draft(store)
    assert first["version"] == first["id"]
    assert first["status"] == "draft"
    assert store.profile()["drafts"] == [first]
    updated = store.update_template(first["id"], {"instructions": "Use compact layout"})
    assert updated["instructions"] == "Use compact layout"
    active = store.activate_template(first["id"], None)
    assert active["status"] == "active"
    assert active["confirmed_at"]
    second = draft(store, "Confirmed API experience")
    assert store.profile()["active"] == active
    with pytest.raises(StoreError) as conflict:
        store.activate_template(second["id"], None)
    assert_status(conflict, 409)
    assert store.profile()["active"] == active
    replacement = store.activate_template(second["id"], active["id"])
    assert store.profile() == {"active": replacement, "drafts": []}
    assert store.template(first["id"])["status"] == "retired"
    with pytest.raises(StoreError) as conflict:
        store.update_template(second["id"], {"instructions": "mutate confirmed"})
    assert_status(conflict, 409)
    with pytest.raises(StoreError) as conflict:
        store.activate_template(second["id"], second["id"])
    assert_status(conflict, 409)


def test_empty_profile_cannot_activate(store: WebStore) -> None:
    template = store.add_template({"facts": []})
    with pytest.raises(StoreError) as error:
        store.activate_template(template["id"], None)
    assert_status(error, 409)
    assert store.profile()["active"] is None


@pytest.mark.parametrize("operation", ["template", "update", "activate"])
def test_missing_templates_are_not_found(store: WebStore, operation: str) -> None:
    with pytest.raises(StoreError) as error:
        if operation == "template":
            store.template("missing")
        elif operation == "update":
            store.update_template("missing", {})
        else:
            store.activate_template("missing", None)
    assert_status(error, 404)


def test_parallel_activation_has_one_winner(store: WebStore) -> None:
    candidates = [draft(store), draft(store)]

    def activate(template):
        try:
            return store.activate_template(template["id"], None)
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(activate, candidates))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert outcomes.count(409) == 1
    assert len(store.profile()["drafts"]) == 1


@pytest.mark.parametrize("values", [{}, {"template_id": "x", "job_id": "y"}])
def test_conversation_requires_exactly_one_scope(store: WebStore, values: dict) -> None:
    with pytest.raises(StoreError) as error:
        store.conversation(**values)
    assert_status(error, 422)


def test_conversations_are_idempotent_and_messages_preserve_order(store: WebStore) -> None:
    job = add_jobs(store, 1)[0]
    conversation = store.conversation(job_id=job.fingerprint)
    assert store.conversation(job_id=job.fingerprint) == conversation
    assert store.messages(conversation["id"]) == []
    first = store.add_message(conversation["id"], "user", "Confirmed fact", "request")
    assert store.add_message(conversation["id"], "user", "Confirmed fact", "request") == first
    answer = store.add_message(conversation["id"], "assistant", "Please confirm", "request")
    assert store.messages(conversation["id"]) == [first, answer]
    with pytest.raises(StoreError) as error:
        store.add_message(conversation["id"], "user", "Different fact", "request")
    assert_status(error, 409)
    with pytest.raises(StoreError) as error:
        store.get_conversation("missing")
    assert_status(error, 404)


def test_task_idempotency_pending_scope_and_private_payload(store: WebStore) -> None:
    job = add_jobs(store, 1)[0]
    payload = {"job_id": job.fingerprint, "conversation_id": "conversation", "secret": "private"}
    first = store.enqueue("cv", "scope", payload, "request")
    assert first["status"] == "queued"
    assert "payload" not in first
    assert first["job_id"] == job.fingerprint
    assert first["conversation_id"] == "conversation"
    assert store.enqueue("cv", "scope", payload, "request") == first
    assert store.enqueue("cv", "scope", payload, "second-request") == first
    assert store.task(first["id"], internal=True)["payload"] == payload
    assert store.get_job(job.fingerprint)["documents"] == [first]
    for kind, changed in [("letter", payload), ("cv", {"different": "data"})]:
        with pytest.raises(StoreError) as error:
            store.enqueue(kind, "scope", changed, "request")
        assert_status(error, 409)
    with pytest.raises(StoreError) as error:
        store.enqueue("cv", "another-scope", payload, "request")
    assert_status(error, 409)
    with pytest.raises(StoreError) as error:
        store.task("missing")
    assert_status(error, 404)


def test_claim_priority_leases_and_terminal_retries(store: WebStore, monkeypatch) -> None:
    monkeypatch.setattr("qa_job_bot.web_store.time.time", lambda: 1000)
    document = store.enqueue("cv", "cv", {}, "doc")
    search = store.enqueue("search", "search", {}, "search")
    claimed = store.claim("worker", lease_seconds=10)
    assert claimed["id"] == search["id"]
    assert claimed["attempts"] == 1
    assert claimed["lease_until"] == 1010
    assert not store.finish_task(search["id"], "other", "ready")
    assert store.finish_task(search["id"], "worker", "ready", {"jobs": 3})
    assert store.task(search["id"])["result"] == {"jobs": 3}
    assert not store.finish_task(search["id"], "worker", "failed")
    assert store.claim("worker")["id"] == document["id"]
    assert store.claim("worker") is None
    assert store.enqueue("search", "search", {}, "retry")["id"] != search["id"]


@pytest.mark.parametrize("intermediate", ["running", "verifying", "rendering"])
def test_expired_lease_fails_and_rejects_late_result(
    store: WebStore, monkeypatch, intermediate
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("qa_job_bot.web_store.time.time", lambda: clock[0])
    task = store.enqueue("cv", "cv", {}, "request")
    store.claim("worker", 10)
    assert store.finish_task(task["id"], "worker", intermediate)
    clock[0] = 1011
    assert not store.finish_task(task["id"], "worker", "ready", {"verified": True})
    assert store.claim("new-worker") is None
    expired = store.task(task["id"])
    assert expired["status"] == "failed"
    assert expired["worker_id"] is None
    assert expired["lease_until"] is None
    assert "прервано" in expired["error"]


def test_parallel_claims_do_not_share_tasks(store: WebStore) -> None:
    tasks = [store.enqueue("cv", f"scope-{i}", {}, f"request-{i}") for i in range(3)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(store.claim, ["one", "two", "three", "four"]))
    assert {result["id"] for result in results if result} == {task["id"] for task in tasks}
    assert sum(result is None for result in results) == 1


@pytest.mark.parametrize("claim_first", [True, False])
def test_cancellation_blocks_late_completion_and_preserves_terminal_state(
    store: WebStore,
    claim_first: bool,
) -> None:
    task = store.enqueue("cv", "scope", {}, "request")
    if claim_first:
        store.claim("worker")
    cancelled = store.cancel_task(task["id"])
    assert cancelled["status"] == "cancelled"
    assert not store.finish_task(task["id"], "worker", "ready", {"verified": True})
    assert store.cancel_task(task["id"])["status"] == "cancelled"
    assert store.claim("worker") is None


@pytest.mark.parametrize(
    "status,verified", [("blocked", False), ("failed", False), ("ready", False), ("ready", True)]
)
def test_artifacts_require_ready_and_verified_task(
    store: WebStore, status: str, verified: bool
) -> None:
    task = store.enqueue("cv", "scope", {}, "request")
    store.claim("worker")
    artifact = store.add_artifact(
        task["id"],
        {"filename": "cv.pdf", "format": "pdf", "path": "/private/cv.pdf", "sha256": "digest"},
    )
    assert store.finish_task(task["id"], "worker", status, {"verified": verified})
    if status == "ready" and verified:
        assert store.artifact(artifact["id"]) == artifact
    else:
        with pytest.raises(StoreError) as error:
            store.artifact(artifact["id"])
        assert_status(error, 409)
    assert store.task(task["id"])["artifacts"] == [
        {"id": artifact["id"], "filename": "cv.pdf", "format": "pdf"}
    ]
    assert store.cancel_task(task["id"])["status"] == status
    with pytest.raises(StoreError) as error:
        store.artifact("missing")
    assert_status(error, 404)


def test_failed_transaction_rolls_back_and_reopens_database(store: WebStore) -> None:
    with pytest.raises(sqlite3.IntegrityError), store._connect() as db:
        db.execute("INSERT INTO web_settings VALUES ('temporary','1')")
        db.execute("INSERT INTO web_settings VALUES ('temporary','2')")
    assert store.get_setting("temporary") is None
    store.set_setting("persisted", True)
    assert store.get_setting("persisted") is True


def test_request_aliases_survive_completion_and_reject_other_payload(store: WebStore) -> None:
    original = store.enqueue("cv", "scope", {}, "first")
    assert store.enqueue("cv", "scope", {}, "second")["id"] == original["id"]
    store.claim("worker")
    store.finish_task(original["id"], "worker", "ready")
    assert store.enqueue("cv", "scope", {}, "second")["id"] == original["id"]
    assert len(store.tasks()) == 1
    with pytest.raises(StoreError) as error:
        store.enqueue("cv", "scope", {"changed": True}, "second")
    assert error.value.status == 409


def test_request_bindings_migrate_existing_tasks_without_losing_history(store: WebStore) -> None:
    old = [
        store.enqueue("cv", "cv:job:template", {"job_id": "job"}, "cv-request"),
        store.enqueue("cover_letter", "letter:job:template", {"job_id": "job"}, "letter-request"),
        store.enqueue(
            "chat",
            "chat:conversation",
            {
                "conversation_id": "conversation",
                "messages": [{"role": "user", "text": "Original message"}],
            },
            "chat-request",
        ),
        store.enqueue("search", "search", {}, "search-request"),
    ]
    with store._connect() as db:
        db.execute("DROP TABLE web_task_requests")
        db.execute("DELETE FROM schema_migrations WHERE version=2")
    store.initialize()
    requests = [
        {"operation": "document", "job_id": "job", "kind": "cv", "language": "en"},
        {"operation": "document", "job_id": "job", "kind": "cover_letter", "language": "auto"},
        {"operation": "chat", "conversation_id": "conversation", "text": "Original message"},
        {"kind": "search", "scope": "search", "payload": {}},
    ]
    for task, request in zip(old, requests, strict=True):
        assert store.request_task(task["request_id"], request)["id"] == task["id"]
    store.initialize()
    assert len(store.tasks()) == 4


def test_parallel_initialization_serializes_migration(store: WebStore) -> None:
    with store._connect() as db:
        db.execute("DROP TABLE web_task_requests")
        db.execute("DELETE FROM schema_migrations WHERE version=2")
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: store.initialize(), range(2)))
    with store._connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=2").fetchone()[0] == 1
        )


def test_parallel_chat_requests_store_exactly_one_message_and_task(store: WebStore) -> None:
    conversation = draft(store)["conversation_id"]

    def send(index):
        try:
            return store.enqueue_message(conversation, {}, f"Message {index}", f"request-{index}")
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, (1, 2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(409) == 1
    assert len(store.tasks()) == 1
    assert len(store.messages(conversation)) == 1
    task = store.tasks()[0]
    message = store.messages(conversation)[0]
    assert (
        store.enqueue_message(
            conversation, {"changed": True}, message["text"], message["request_id"]
        )["id"]
        == task["id"]
    )


def test_chat_task_failure_rolls_back_user_message(store: WebStore) -> None:
    conversation = draft(store)["conversation_id"]
    with store._connect() as db:
        db.execute("""CREATE TRIGGER reject_task BEFORE INSERT ON web_tasks
            BEGIN SELECT RAISE(ABORT,'simulated disk error'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated disk error"):
        store.enqueue_message(conversation, {}, "Do not orphan this", "request")
    assert store.messages(conversation) == []
    assert store.tasks() == []
    assert store.request_task("request", {}) is None


def test_chat_completion_is_atomic_and_history_is_bounded(store: WebStore) -> None:
    conversation = draft(store)["conversation_id"]
    for index in range(12):
        store.add_message(conversation, "user", f"Message {index}", f"history-{index}")
    task = store.enqueue_message(conversation, {"template": "snapshot"}, "Newest", "request")
    messages = store.task(task["id"], internal=True)["payload"]["messages"]
    assert [message["text"] for message in messages] == [
        f"Message {index}" for index in range(4, 12)
    ] + ["Newest"]
    store.claim("worker")
    assert not store.finish_chat(task["id"], "other-worker", "Wrong reply")
    with store._connect() as db:
        db.execute("""CREATE TRIGGER reject_assistant BEFORE INSERT ON web_messages
            WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'reply storage failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="reply storage failed"):
        store.finish_chat(task["id"], "worker", "Answer")
    assert store.task(task["id"])["status"] == "running"
    assert all(message["role"] == "user" for message in store.messages(conversation))
    with store._connect() as db:
        db.execute("DROP TRIGGER reject_assistant")
    assert store.finish_chat(task["id"], "worker", "Answer")
    assert store.task(task["id"])["status"] == "ready"
    assert store.messages(conversation)[-1]["text"] == "Answer"
    assert not store.finish_chat(task["id"], "worker", "Duplicate")


def test_old_job_documents_remain_after_many_other_tasks(store: WebStore) -> None:
    job = add_jobs(store, 1)[0]
    document = store.enqueue("cv", "original", {"job_id": job.fingerprint}, "original")
    for index in range(201):
        store.enqueue("search", f"scope-{index}", {}, f"request-{index}")
    assert len(store.tasks()) == 200
    assert document["id"] not in {task["id"] for task in store.tasks()}
    assert [task["id"] for task in store.get_job(job.fingerprint)["documents"]] == [document["id"]]


def test_owner_messages_are_scoped_recent_and_exclude_assistant_text(store: WebStore) -> None:
    jobs = add_jobs(store, 2)
    assert store.owner_messages(jobs[0].fingerprint) == []
    conversation = store.conversation(job_id=jobs[0].fingerprint)["id"]
    other = store.conversation(job_id=jobs[1].fingerprint)["id"]
    for index in range(10):
        store.add_message(conversation, "user", f"Preference {index}", f"request-{index}")
        store.add_message(conversation, "assistant", f"Reply {index}", f"request-{index}")
    store.add_message(other, "user", "Other job preference", "other")
    assert store.owner_messages(jobs[0].fingerprint) == [
        f"Preference {index}" for index in range(2, 10)
    ]
    assert store.owner_messages(jobs[1].fingerprint) == ["Other job preference"]
