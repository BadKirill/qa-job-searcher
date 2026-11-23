import json
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from legacy_fixtures import PASSWORD, seed_legacy
from pg_fixtures import personal_store
from sqlalchemy import func, select, update

from qa_job_bot import legacy_import as importing
from qa_job_bot import pg_schema as s
from qa_job_bot.legacy_cli import bootstrap_owner, main
from qa_job_bot.legacy_import import restore_snapshot
from qa_job_bot.legacy_plan import build_plan, entity_id, json_object, timestamp
from qa_job_bot.legacy_snapshot import ImportFailure, create_snapshot, sha256
from qa_job_bot.pg_queue import QueueRepository
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings


@pytest.fixture(scope="session")
def legacy(tmp_path_factory):
    return seed_legacy(tmp_path_factory.mktemp("legacy-source"))


def owner(database, login="import-owner"):
    return database.create_user(login, login)["id"]


def restore(legacy, database, user_id, target, **kwargs):
    return restore_snapshot(legacy["snapshot"]["root"], database, user_id, target, **kwargs)


def count(database, table):
    with database.engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_restore_preserves_history_files_proofs_and_private_configuration(
    legacy, pg_database, tmp_path
):
    user_id = owner(pg_database)
    other = personal_store(pg_database, "other")
    other.set_setting("private", {"keep": True})
    other.save_session("other-session", "csrf", 0, 99999999999)
    store = PostgresStore(pg_database, user_id)
    store.save_session("target-session", "csrf", 0, 99999999999)
    target = tmp_path / "restored"
    before = legacy["store"].path.read_bytes()
    report = restore(legacy, pg_database, user_id, target, telegram_chats=[101])
    assert report["verified_counts"] == report["target_counts"]
    assert report["source_sessions_revoked"] == 1 and report["target_sessions_revoked"] == 1
    assert report["unlinked_telegram"] == 4
    assert report["incomplete_jobs"] == [legacy["jobs"][3].fingerprint]
    assert legacy["store"].path.read_bytes() == before
    assert store.get_setting("password") == legacy["store"].get_setting("password")
    assert not store.find_session("target-session", 1) and other.find_session("other-session", 1)
    assert other.get_setting("private") == {"keep": True}
    for original in legacy["jobs"]:
        current = store.get_job(original.fingerprint)
        old = (
            next(
                row
                for row in legacy["snapshot"]["tables"]["web_state"]
                if row["job_id"] == original.fingerprint
            )
            if original != legacy["jobs"][0]
            else {"applied_at": None, "archived_at": None}
        )
        assert current["id"] == original.fingerprint and current["score"] == original.score
        assert current["applied_at"] == old["applied_at"]
        assert current["archived_at"] == old["archived_at"]
    current_template = store.template(legacy["template"]["id"])
    for key in ("facts", "reference", "version", "confirmed_at", "created_at"):
        assert current_template[key] == legacy["template"][key]
    assert current_template["path"] == str(target / "templates/candidate.pdf")
    old_messages = legacy["store"].messages(legacy["conversation"]["id"])
    current_messages = store.messages(legacy["conversation"]["id"])
    assert [
        {key: message[key] for key in old_messages[0]} for message in current_messages
    ] == old_messages
    for task in legacy["documents"]:
        saved = store.task(task["id"], internal=True)
        assert saved["status"] == "ready" and saved["result"] == task["result"]
        assert saved["payload"]["context"]["owner_id"] == user_id
        assert saved["payload"]["context"]["template"]["version"] == legacy["template"]["version"]
        for artifact in task["artifacts"]:
            original = legacy["store"].artifact(artifact["id"])
            imported = store.artifact(artifact["id"])
            assert Path(imported["path"]).read_bytes() == Path(original["path"]).read_bytes()
            assert imported["sha256"] == sha256(Path(imported["path"]).read_bytes())
    assert store.task(legacy["running"]["id"])["status"] == "failed"
    assert store.task(legacy["doubtful"]["id"])["status"] == "blocked"
    assert store.task(legacy["queued"]["id"])["status"] == "queued"
    queue = QueueRepository(pg_database)
    assert queue.claim("new-worker")["id"] == legacy["queued"]["id"]
    assert queue.claim("another-worker") is None
    for entry in legacy["snapshot"]["manifest"]["files"]:
        assert sha256((target / entry["path"]).read_bytes()) == entry["sha256"]
    assert not (target / "setup-token").exists() and not (target / ".env").exists()
    with pg_database.engine.connect() as connection:
        assert connection.execute(select(s.user_sources.c.user_id)).scalars().all() == [user_id] * 7
        assert connection.execute(select(s.search_profiles.c.payload)).scalar_one()[
            "role_keywords"
        ] == ["qa engineer"]
        assert connection.execute(select(s.telegram_bindings.c.chat_id)).scalars().all() == [101]
        assert connection.execute(select(s.deliveries.c.job_id)).scalars().all() == [
            j.fingerprint for j in legacy["jobs"][:2]
        ]
        assert all(
            "score" not in row for row in connection.execute(select(s.jobs.c.payload)).scalars()
        )
    assert (
        store.list_jobs("active", 1, legacy["snapshot_id"])["snapshot_id"] == legacy["snapshot_id"]
    )


def test_historical_source_aliases_keep_ids_states_documents_and_explicit_mapping(
    legacy, pg_database, tmp_path
):
    database_path = tmp_path / "historical.sqlite3"
    original_id = legacy["jobs"][0].fingerprint
    alias_id = "historical-source-version"
    with (
        closing(sqlite3.connect(legacy["store"].path)) as source,
        closing(sqlite3.connect(database_path)) as copied,
    ):
        source.backup(copied)
        copied.execute(
            "INSERT INTO jobs (fingerprint,source,source_id,title,company,location,url,"
            "published_at,score,first_seen_at,last_seen_at,payload) "
            "SELECT ?,source,source_id,?,company,location,url,published_at,score,"
            "first_seen_at,last_seen_at,payload FROM jobs WHERE fingerprint=?",
            (alias_id, "Earlier historical title", original_id),
        )
        copied.execute(
            "INSERT INTO web_state VALUES (?,NULL,?)",
            (alias_id, "2026-01-01T12:00:00+00:00"),
        )
        copied.commit()
    snapshot = create_snapshot(
        database_path, legacy["settings"].data_dir, legacy["config"], tmp_path / "snapshot"
    )
    user_id = owner(pg_database)
    target = tmp_path / "restored"
    report = restore_snapshot(snapshot["root"], pg_database, user_id, target)
    store = PostgresStore(pg_database, user_id)
    assert count(pg_database, s.jobs) == len(legacy["jobs"]) + 1
    assert count(pg_database, s.identities) == len(legacy["jobs"])
    assert store.get_job(alias_id)["archived_at"]
    assert not store.get_job(original_id)["archived_at"]
    assert store.task(legacy["documents"][0]["id"])["job_id"] == original_id
    assert {"code": "historical_identity_alias", "id": alias_id} in report["issues"]
    with pg_database.engine.connect() as connection:
        mapped = connection.execute(
            select(s.identities.c.job_id).where(
                s.identities.c.source_key
                == f"{legacy['jobs'][0].source.casefold()}\x1f{legacy['jobs'][0].source_id}"
            )
        ).scalar_one()
    assert mapped == original_id
    assert restore_snapshot(snapshot["root"], pg_database, user_id, target)["already_imported"]


@pytest.mark.parametrize("selected_first", [True, False])
def test_historical_alias_selection_does_not_depend_on_row_order(legacy, tmp_path, selected_first):
    snapshot = deepcopy(legacy["snapshot"])
    original = snapshot["tables"]["jobs"][0]
    alias = {**original, "fingerprint": "historical-alias", "title": "Earlier title"}
    rows = snapshot["tables"]["jobs"]
    rows.append(alias) if selected_first else rows.insert(0, alias)
    plan = build_plan(snapshot, "owner", tmp_path)
    assert len(plan["rows"]["jobs"]) == len(legacy["jobs"]) + 1
    assert {"code": "historical_identity_alias", "id": alias["fingerprint"]} in plan["report"][
        "issues"
    ]


@pytest.mark.parametrize("problem", ["different-url", "different-key", "missing-mapping"])
def test_historical_aliases_require_a_consistent_explicit_identity(legacy, tmp_path, problem):
    snapshot = deepcopy(legacy["snapshot"])
    original = snapshot["tables"]["jobs"][0]
    alias = {**original, "fingerprint": "historical-alias"}
    snapshot["tables"]["jobs"].append(alias)
    if problem == "different-url":
        alias["url"] = "https://example.com/unrelated"
    elif problem == "different-key":
        original["source_id"] = "different-source-identifier"
    else:
        snapshot["tables"]["job_identities"] = []
    with pytest.raises(ImportFailure) as error:
        build_plan(snapshot, "owner", tmp_path)
    assert error.value.code == "identity_conflict"


def test_check_and_replay_never_duplicate_or_reset_new_state(legacy, pg_database, tmp_path):
    user_id = owner(pg_database)
    target = tmp_path / "target"
    checked = restore(legacy, pg_database, user_id, target, check_only=True)
    assert checked["checked"] and not target.exists() and count(pg_database, s.jobs) == 0
    first = restore(legacy, pg_database, user_id, target)
    store = PostgresStore(pg_database, user_id)
    store.change_state(legacy["jobs"][0].fingerprint, {"applied": True})
    store.save_session("new-session", "new-csrf", 0, 99999999999)
    totals = {name: count(pg_database, s.metadata.tables[name]) for name in importing.ORDER}
    assert restore(legacy, pg_database, user_id, target) == {**first, "already_imported": True}
    assert totals == {name: count(pg_database, s.metadata.tables[name]) for name in importing.ORDER}
    assert store.get_job(legacy["jobs"][0].fingerprint)["applied_at"]
    assert store.find_session("new-session", 1)
    with pytest.raises(ImportFailure, match="аккаунту"):
        restore(legacy, pg_database, owner(pg_database, "second"), target)
    with pytest.raises(ImportFailure, match="директорией"):
        restore(legacy, pg_database, user_id, tmp_path / "different")
    (target / "templates/candidate.pdf").write_bytes(b"tampered")
    with pytest.raises(ImportFailure) as error:
        restore(legacy, pg_database, user_id, target)
    assert error.value.code == "destination_integrity"


def test_import_failure_rolls_back_database_and_private_files_and_can_retry(
    legacy, pg_database, tmp_path, monkeypatch
):
    user_id = owner(pg_database)
    target = tmp_path / "target"
    original = importing.verify_destination
    calls = []

    def broken_publication(path, marker):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("synthetic publication failure")
        return original(path, marker)

    monkeypatch.setattr(importing, "verify_destination", broken_publication)
    with pytest.raises(RuntimeError, match="publication"):
        restore(legacy, pg_database, user_id, target)
    assert not target.exists() and count(pg_database, s.jobs) == 0
    assert count(pg_database, s.import_runs) == 0
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(importing, "verify_destination", original)
    assert not restore(legacy, pg_database, user_id, target)["already_imported"]


def test_recovers_published_files_after_interrupted_transaction(legacy, pg_database, tmp_path):
    user_id = owner(pg_database)
    target = tmp_path / "target"
    marker = importing.marker_data(legacy["snapshot"], user_id, [])
    staging = importing.stage_files(legacy["snapshot"], target, marker)
    staging.rename(target)
    assert count(pg_database, s.import_runs) == 0
    assert not restore(legacy, pg_database, user_id, target)["already_imported"]
    assert restore(legacy, pg_database, user_id, target)["already_imported"]


@pytest.mark.parametrize(
    "problem",
    ["missing-owner", "disabled", "not-empty", "password", "destination", "overlap", "symlink"],
)
def test_rejects_unsafe_target_without_writes(legacy, pg_database, tmp_path, problem):
    user_id = owner(pg_database)
    target = tmp_path / "target"
    with pg_database.transaction() as connection:
        if problem == "disabled":
            connection.execute(update(s.users).where(s.users.c.id == user_id).values(disabled=True))
        elif problem == "not-empty":
            connection.execute(s.settings.insert().values(user_id=user_id, key="keep", value=1))
        elif problem == "password":
            connection.execute(
                update(s.users).where(s.users.c.id == user_id).values(password_hash="existing")
            )
    if problem == "missing-owner":
        user_id = "unknown-owner"
    if problem == "destination":
        target.mkdir()
        (target / importing.MARKER).write_text("{}")
    if problem == "overlap":
        target = legacy["snapshot"]["root"] / "inside"
    if problem == "symlink":
        target.symlink_to(legacy["snapshot"]["root"], target_is_directory=True)
    with pytest.raises(ImportFailure):
        restore(legacy, pg_database, user_id, target)
    assert count(pg_database, s.jobs) == 0 and count(pg_database, s.import_runs) == 0


def test_global_identity_conflict_rolls_back_without_overwriting_another_owner(
    legacy, pg_database, tmp_path
):
    other = personal_store(pg_database, "other")
    other.upsert_jobs(legacy["jobs"][:1])
    with pg_database.transaction() as connection:
        connection.execute(update(s.jobs).values(title="Other source update"))
    with pytest.raises(ImportFailure) as error:
        restore(legacy, pg_database, owner(pg_database), tmp_path / "target")
    assert error.value.code == "public_identity_conflict"
    assert count(pg_database, s.jobs) == 1
    assert other.get_job(legacy["jobs"][0].fingerprint)["title"] == legacy["jobs"][0].title
    assert not (tmp_path / "target").exists()


def test_bootstrap_is_empty_installation_only_and_preserves_private_setup(pg_database, tmp_path):
    for login, display in [("", "Owner"), ("owner", "")]:
        with pytest.raises(ImportFailure):
            bootstrap_owner(pg_database, login, display)
    result = bootstrap_owner(pg_database, "  ＯWNER  ", " Owner ")
    user = pg_database.user(result["user_id"])
    assert user["login"] == "owner" and user["role"] == "admin"
    app = create_app(
        WebSettings(tmp_path, ("http://testserver",)), PostgresStore(pg_database, user["id"])
    )
    assert app.state.security.setup_file.stat().st_mode & 0o777 == 0o600
    assert app.state.security.store.get_setting("password") is None
    with pytest.raises(ImportFailure) as error:
        bootstrap_owner(pg_database, "second", "Second")
    assert error.value.code == "bootstrap_completed" and count(pg_database, s.users) == 1


def test_imported_api_login_downloads_history_and_refuses_incomplete_or_unverified(
    legacy, pg_database, tmp_path
):
    user_id = owner(pg_database)
    target = tmp_path / "target"
    restore(legacy, pg_database, user_id, target)
    store = PostgresStore(pg_database, user_id)
    app = create_app(WebSettings(target, ("http://testserver",)), store)
    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        assert not client.get("/api/session").json()["setup_required"]
        client.cookies.set("qa_session", "legacy-valid-token")
        assert client.get("/api/jobs").status_code == 401
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        response = client.post("/api/login", json={"password": PASSWORD})
        assert response.status_code == 200
        client.headers["x-csrf-token"] = response.json()["csrf_token"]
        applied = client.get("/api/jobs?tab=applied").json()["items"]
        assert len(applied) == 2
        incomplete = next(row for row in applied if row.get("needs_refresh"))
        assert (
            client.post(
                f"/api/jobs/{incomplete['id']}/documents",
                json={"kind": "cv", "language": "en", "request_id": "new-incomplete"},
            ).status_code
            == 409
        )
        assert (
            client.get("/api/jobs?tab=archive").json()["items"][0]["id"]
            == legacy["jobs"][2].fingerprint
        )
        assert (
            client.get(f"/api/conversations/{legacy['conversation']['id']}/messages").json()[
                "items"
            ][0]["text"]
            == "Confirmed historical instruction"
        )
        assert (
            client.get(f"/api/templates/{legacy['template']['id']}/source").content
            == (target / "templates/candidate.pdf").read_bytes()
        )
        for task in legacy["documents"]:
            assert client.get(f"/api/tasks/{task['id']}/document-preview").status_code == 200
            for artifact in task["artifacts"]:
                response = client.get(f"/api/artifacts/{artifact['id']}/download")
                assert (
                    response.status_code == 200
                    and response.content
                    == Path(store.artifact(artifact["id"])["path"]).read_bytes()
                )
        assert (
            client.get(f"/api/tasks/{legacy['doubtful']['id']}/document-preview").status_code == 404
        )
        assert (
            client.get(f"/api/artifacts/{legacy['doubtful_artifact']['id']}/download").status_code
            == 409
        )
        assert client.get("/api/templates/foreign-template/source").status_code == 404
        old = legacy["documents"][0]
        repeated = client.post(
            f"/api/jobs/{legacy['jobs'][0].fingerprint}/documents",
            json={"kind": "cv", "language": "en", "request_id": "legacy-cv-request"},
        )
        assert repeated.status_code == 202 and repeated.json()["id"] == old["id"]


@pytest.mark.parametrize("value", [None, 3, "", "a/b", "..", "a" * 129])
def test_plan_rejects_invalid_identifiers(value):
    with pytest.raises(ImportFailure):
        entity_id(value)


def test_plan_scalar_validation():
    assert timestamp("2026-01-01").utcoffset().total_seconds() == 0
    for value in (None, "wrong-date"):
        with pytest.raises(ImportFailure):
            timestamp(value)
    with pytest.raises(ImportFailure):
        json_object("[]")


@pytest.mark.parametrize(
    "problem,code",
    [
        ("job-fields", "unsupported_job"),
        ("score", "invalid_score"),
        ("identity", "identity_conflict"),
        ("state", "broken_reference"),
        ("template-id", "template_conflict"),
        ("facts", "invalid_profile"),
        ("conversation", "broken_reference"),
        ("message", "broken_reference"),
        ("owner", "owner_mismatch"),
        ("artifact-id", "broken_reference"),
        ("artifact-file", "missing_reference_file"),
        ("hash", "reference_integrity"),
        ("password", "invalid_password_hash"),
        ("revision", "invalid_revision"),
        ("settings", "broken_reference"),
        ("snapshot", "broken_reference"),
        ("telegram", "unknown_telegram_chat"),
        ("config", "invalid_configuration"),
    ],
)
def test_plan_rejects_malformed_source_references(legacy, tmp_path, problem, code):
    snapshot = deepcopy(legacy["snapshot"])
    tables = snapshot["tables"]
    if problem in {"job-fields", "score"}:
        row = tables["jobs"][0]
        if problem == "score":
            row["score"] = 101
        else:
            row["payload"] = '{"foreign_secret": 1}'
    elif problem == "identity":
        tables["job_identities"][0]["fingerprint"] = "other"
    elif problem == "state":
        tables["web_state"][0]["job_id"] = "other"
    elif problem in {"template-id", "facts", "hash"}:
        data = json.loads(tables["web_templates"][0]["payload"])
        data.update(
            {"id": "wrong"}
            if problem == "template-id"
            else {"facts": []}
            if problem == "facts"
            else {"source_sha256": "changed"}
        )
        tables["web_templates"][0]["payload"] = json.dumps(data)
    elif problem == "conversation":
        tables["web_conversations"][0]["payload"] = '{"id": "wrong"}'
    elif problem == "message":
        tables["web_messages"][0]["conversation_id"] = "missing"
    elif problem == "owner":
        data = json.loads(tables["web_tasks"][0]["payload"])
        data["context"]["owner_id"] = "other"
        tables["web_tasks"][0]["payload"] = json.dumps(data)
    elif problem in {"artifact-id", "artifact-file"}:
        data = json.loads(tables["web_artifacts"][0]["payload"])
        data.update({"id": "wrong"} if problem == "artifact-id" else {"path": None})
        tables["web_artifacts"][0]["payload"] = json.dumps(data)
    elif problem in {"password", "revision", "settings"}:
        key, value = {
            "password": ("password", "plain"),
            "revision": ("revision", -1),
            "settings": ("current_jobs", ["missing"]),
        }[problem]
        next(row for row in tables["web_settings"] if row["key"] == key)["value"] = json.dumps(
            value
        )
    elif problem == "snapshot":
        tables["web_snapshots"][0]["job_ids"] = '["missing"]'
    elif problem == "config":
        snapshot["configuration"]["search_profile.toml"] = b"invalid ["
    with pytest.raises(ImportFailure) as error:
        build_plan(snapshot, "owner", tmp_path, [999] if problem == "telegram" else [])
    assert error.value.code == code


def test_cli_rehearsal_outputs_only_safe_report(legacy, pg_database, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", pg_database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DATABASE_SCHEMA", pg_database.schema)
    assert main(["bootstrap", "--login", "owner", "--display-name", "Owner"]) == 0
    user_id = json.loads(capsys.readouterr().out)["user_id"]
    args = [
        "--snapshot",
        str(legacy["snapshot"]["root"]),
        "--owner-id",
        user_id,
        "--target-dir",
        str(tmp_path / "target"),
    ]
    assert main(["check", *args]) == 0
    assert json.loads(capsys.readouterr().out)["checked"]
    assert main(["restore", *args]) == 0
    output = capsys.readouterr().out
    assert not json.loads(output)["already_imported"]
    for secret in (
        PASSWORD,
        "local-test-only",
        "old-session-hash",
        "Confirmed historical instruction",
    ):
        assert secret not in output
    assert main(["bootstrap", "--login", "second", "--display-name", "Second"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "bootstrap_completed"}


def test_identical_public_jobs_are_reused_and_check_detects_conflicts(
    legacy, pg_database, tmp_path
):
    other = personal_store(pg_database, "other")
    other.upsert_jobs(legacy["jobs"][:1])
    user_id = owner(pg_database)
    report = restore(legacy, pg_database, user_id, tmp_path / "target", check_only=True)
    assert report["reused_public_jobs"] == 1 and count(pg_database, s.jobs) == 1
    report = restore(legacy, pg_database, user_id, tmp_path / "target")
    assert report["reused_public_jobs"] == 1 and count(pg_database, s.jobs) == 4
    assert other.get_job(legacy["jobs"][0].fingerprint)["score"] == 90


@pytest.mark.parametrize(
    "corruption",
    [
        "verified",
        "draft-hash",
        "version",
        "review",
        "artifact-ids",
        "pdf",
        "artifact-hash",
        "visual",
    ],
)
def test_only_original_valid_document_proofs_keep_ready(legacy, tmp_path, corruption):
    snapshot = deepcopy(legacy["snapshot"])
    task = snapshot["tables"]["web_tasks"][0]
    result = json.loads(task["result"])
    if corruption == "verified":
        result["verified"] = False
    elif corruption == "draft-hash":
        result["draft_hash"] = "bad"
    elif corruption == "version":
        result["template_version"] = "different"
    elif corruption == "review":
        result["review"].update(verdict="revise", issues=["Requires correction"])
    elif corruption == "artifact-ids":
        result["artifact_ids"] = []
    elif corruption in {"pdf", "artifact-hash"}:
        for artifact in snapshot["tables"]["web_artifacts"]:
            if artifact["task_id"] == task["id"]:
                data = json.loads(artifact["payload"])
                data.update({"format": "txt"} if corruption == "pdf" else {"draft_hash": "bad"})
                artifact["payload"] = json.dumps(data)
    else:
        result["visual_review"]["rendered_sha256"] = "bad"
    task["result"] = json.dumps(result)
    plan = build_plan(snapshot, "owner", tmp_path)
    restored = next(row for row in plan["rows"]["tasks"] if row["id"] == task["id"])
    assert restored["status"] == "blocked"
    assert restored["result"] == result
    assert {"code": "document_verification_required", "id": task["id"]} in plan["report"]["issues"]


def test_queued_duplicates_and_invalid_context_are_not_executed_and_old_requests_reconstruct(
    legacy, tmp_path
):
    snapshot = deepcopy(legacy["snapshot"])
    tables = snapshot["tables"]
    queued = deepcopy(
        next(row for row in tables["web_tasks"] if row["id"] == legacy["queued"]["id"])
    )
    duplicate = {**queued, "id": "duplicate-queued", "request_id": "duplicate-request"}
    bad = {
        **queued,
        "kind": "cv",
        "scope": "bad-context",
        "id": "bad-queued",
        "request_id": "bad-request",
    }
    tables["web_tasks"].extend([duplicate, bad])
    tables["web_task_requests"] = []
    chat = {
        **queued,
        "kind": "chat",
        "scope": "old-chat",
        "id": "old-chat-task",
        "request_id": "old-chat-request",
        "status": "ready",
        "payload": json.dumps(
            {
                "conversation_id": legacy["conversation"]["id"],
                "messages": [{"text": "Old instruction"}],
            }
        ),
    }
    tables["web_tasks"].append(chat)
    plan = build_plan(snapshot, "owner", tmp_path, [101, 202, 303])
    by_id = {row["id"]: row for row in plan["rows"]["tasks"]}
    assert by_id[queued["id"]]["status"] == "queued"
    assert by_id[duplicate["id"]]["status"] == "blocked"
    assert by_id[bad["id"]]["status"] == "blocked"
    request = next(row for row in plan["rows"]["task_requests"] if row["task_id"] == chat["id"])
    assert (
        request["payload"]["operation"] == "chat"
        and request["payload"]["text"] == "Old instruction"
    )
    assert [row["enabled"] for row in plan["rows"]["telegram_bindings"]] == [True, False, False]
    assert plan["report"]["unlinked_telegram"] == 1


def test_cli_snapshot_and_module_entrypoint(legacy, pg_database, tmp_path, monkeypatch, capsys):
    import runpy

    assert (
        main(
            [
                "snapshot",
                "--database",
                str(legacy["store"].path),
                "--data-dir",
                str(legacy["settings"].data_dir),
                "--config-dir",
                str(legacy["config"]),
                "--output",
                str(tmp_path / "snapshot"),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["source_hash"] == legacy["snapshot"]["manifest"]["source_hash"]
    monkeypatch.setenv("DATABASE_URL", pg_database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DATABASE_SCHEMA", pg_database.schema)
    monkeypatch.setattr(
        "sys.argv", ["qa-job-import", "bootstrap", "--login", "owner", "--display-name", "Owner"]
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_module("qa_job_bot.legacy_cli", run_name="__main__")
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["user_id"]


def test_duplicate_explicit_telegram_arguments_do_not_duplicate_bindings(
    legacy, pg_database, tmp_path
):
    report = restore(
        legacy, pg_database, owner(pg_database), tmp_path / "target", telegram_chats=[101, 101]
    )
    assert report["target_counts"]["telegram_bindings"] == 1
    assert count(pg_database, s.telegram_bindings) == 1


@pytest.mark.parametrize("check_only", [True, False])
def test_database_constraints_reject_corrupt_history_without_partial_publish(
    legacy, pg_database, tmp_path, check_only
):
    import shutil
    import sqlite3
    from contextlib import closing

    from qa_job_bot.legacy_snapshot import database_tables, snapshot_identity
    from qa_job_bot.web_store import digest

    root = tmp_path / "bad-snapshot"
    shutil.copytree(legacy["snapshot"]["root"], root)
    with closing(sqlite3.connect(root / "legacy.sqlite3")) as connection:
        connection.execute("UPDATE web_messages SET role='system' WHERE role='assistant'")
        connection.commit()
    manifest = json.loads((root / "manifest.json").read_text())
    content = (root / "legacy.sqlite3").read_bytes()
    manifest.update(database_sha256=sha256(content), tables_sha256=digest(database_tables(content)))
    manifest["source_hash"] = snapshot_identity(manifest)
    (root / "manifest.json").write_text(json.dumps(manifest))
    user_id = owner(pg_database)
    with pytest.raises(ImportFailure) as error:
        restore_snapshot(root, pg_database, user_id, tmp_path / "target", check_only=check_only)
    assert error.value.code == "database_rejected"
    assert count(pg_database, s.jobs) == 0 and count(pg_database, s.import_runs) == 0
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(".target.import-*"))


@pytest.mark.parametrize("saved", [True, False])
@pytest.mark.parametrize("transport", ["sqlalchemy", "os"])
def test_uncertain_commit_retains_files_and_reconciles_actual_database(
    legacy, pg_database, tmp_path, monkeypatch, saved, transport
):
    from sqlalchemy.exc import OperationalError

    user_id = owner(pg_database)
    target = tmp_path / "target"
    original = pg_database.transaction

    def disconnected():
        return (
            OperationalError("COMMIT", None, ConnectionError("lost acknowledgement"))
            if transport == "sqlalchemy"
            else OSError("lost acknowledgement")
        )

    @contextmanager
    def interrupted():
        with original() as connection:
            yield connection
            if not saved:
                raise disconnected()
        raise disconnected()

    monkeypatch.setattr(pg_database, "transaction", interrupted)
    with pytest.raises(ImportFailure) as error:
        restore(legacy, pg_database, user_id, target)
    assert error.value.code == "commit_outcome_unknown"
    assert target.is_dir()
    for entry in legacy["snapshot"]["manifest"]["files"]:
        assert sha256((target / entry["path"]).read_bytes()) == entry["sha256"]
    assert count(pg_database, s.jobs) == (4 if saved else 0)
    assert count(pg_database, s.import_runs) == int(saved)
    monkeypatch.setattr(pg_database, "transaction", original)
    assert restore(legacy, pg_database, user_id, target)["already_imported"] is saved
    assert count(pg_database, s.jobs) == 4 and count(pg_database, s.import_runs) == 1
