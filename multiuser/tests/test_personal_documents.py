import asyncio
import io
import json
import time
from pathlib import Path

import httpx
import pytest
from alembic import command
from docx import Document
from e2e_server import FixtureGateway
from pg_fixtures import personal_store
from pypdf import PdfReader
from sqlalchemy import inspect, select
from test_accounts import api_world as api_world

from qa_job_bot import pg_schema as s
from qa_job_bot.agent_gateway import AgentGateway, AgentUnavailable
from qa_job_bot.agent_result import AgentResponse
from qa_job_bot.document_files import DocumentFileError
from qa_job_bot.document_policy import content_hash
from qa_job_bot.mcp_server import TaskScope, create_server, scope_context
from qa_job_bot.models import Job
from qa_job_bot.openai_config import OpenAISettings
from qa_job_bot.openai_transport import OpenAITransport
from qa_job_bot.personal_files import allowed_file, owner_root, private_directory
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError
from qa_job_bot.worker import Worker


def vacancy(description="Build reliable backend software with Python."):
    return Job(
        "Synthetic",
        "shared",
        "Backend Engineer",
        "Acme",
        "Worldwide",
        "https://example.test/job",
        description=description,
        score=80,
    )


def template(store, text="Alex Example builds Python services."):
    draft = store.add_template(
        {
            "facts": [{"id": "identity", "text": text, "source": "User confirmation"}],
            "filename": "synthetic.docx",
        }
    )
    return store.activate_template(
        draft["id"], store.profile()["active"]["id"] if store.profile()["active"] else None
    )


def seed(store, text="Alex Example builds Python services."):
    job = vacancy()
    store.upsert_jobs([job])
    store.publish_search([job], [])
    return job.fingerprint, template(store, text)


def context(owner="alice", task="task-a"):
    return {
        "owner_id": owner,
        "task_id": task,
        "kind": "cv",
        "language": "en",
        "job": {"title": "Product Designer"},
        "template": {
            "id": "template-" + owner,
            "version": "version-" + owner,
            "facts": [{"id": "same-id", "text": owner + " confirmed design experience."}],
        },
    }


def test_document_inputs_pin_historical_job_template_facts_and_own_messages(pg_store):
    job_id, first_template = seed(pg_store)
    first = pg_store.list_jobs("active", 1)
    conversation = pg_store.conversation(job_id=job_id, snapshot_id=first["snapshot_id"])
    pg_store.add_message(conversation["id"], "user", "Keep the original role.", "message-1")
    task = pg_store.enqueue_document(
        job_id, "cv", "en", "generate-first", snapshot_id=first["snapshot_id"]
    )
    newer = vacancy("Changed requirements for another role.")
    pg_store.upsert_jobs([newer])
    pg_store.publish_search([newer], [])
    second_template = template(pg_store, "Alex Example designs accessible products.")
    pg_store.add_message(conversation["id"], "user", "This message arrived later.", "message-2")
    pinned = pg_store.task(task["id"], internal=True)["payload"]["context"]
    assert pinned["owner_id"] == pg_store.user_id
    assert pinned["job"]["description"] == vacancy().description
    assert pinned["template"]["id"] == first_template["id"] != second_template["id"]
    assert pinned["owner_messages"] == ["Keep the original role."]
    assert pinned["job_version"]["snapshot_id"] == first["snapshot_id"]
    assert (
        pg_store.enqueue_document(
            job_id, "cv", "en", "generate-first", snapshot_id=first["snapshot_id"]
        )["id"]
        == task["id"]
    )
    current = pg_store.enqueue_document(job_id, "cv", "en", "generate-new")
    assert current["id"] != task["id"]
    assert (
        pg_store.task(current["id"], internal=True)["payload"]["context"]["job"]["description"]
        == newer.description
    )


def test_job_conversations_are_versioned_and_draft_updates_do_not_change_active_cv(pg_store):
    job_id, active = seed(pg_store)
    old = pg_store.list_jobs("active", 1)
    first = pg_store.conversation(job_id=job_id, snapshot_id=old["snapshot_id"])
    assert pg_store.conversation(job_id=job_id)["id"] == first["id"]
    pg_store.add_message(first["id"], "user", "Original discussion", "one")
    newer = vacancy("New product design responsibilities.")
    pg_store.upsert_jobs([newer])
    pg_store.publish_search([newer], [])
    second = pg_store.conversation(job_id=job_id)
    assert first["id"] != second["id"] and pg_store.messages(second["id"]) == []
    task = pg_store.enqueue_personal_message(first["id"], "Refine this version.", "chat")
    assert (
        pg_store.task(task["id"], internal=True)["payload"]["context"]["job"]["description"]
        == vacancy().description
    )
    document = pg_store.enqueue_document(
        job_id, "cv", "en", "from-conversation", conversation_id=first["id"]
    )
    assert (
        pg_store.task(document["id"], internal=True)["payload"]["context"]["conversation_messages"][
            -1
        ]["text"]
        == "Refine this version."
    )
    draft = pg_store.add_template({"facts": [{"id": "new", "text": "New unconfirmed text."}]})
    pg_store.update_template(draft["id"], {"instructions": "Draft-only changes"})
    assert pg_store.profile()["active"]["id"] == active["id"]
    with pytest.raises(StoreError) as error:
        pg_store.enqueue_document(
            job_id,
            "cv",
            "en",
            "mismatch",
            conversation_id=first["id"],
            run_id=pg_store.get_setting("current_search_run"),
        )
    assert error.value.status == 409


def test_foreign_snapshots_runs_templates_and_conversations_never_choose_document_context(pg_store):
    other = personal_store(pg_store.database, "bob-docs")
    job_id, _ = seed(pg_store)
    seed(other, "Bob Example manages design systems.")
    foreign = other.list_jobs("active", 1)
    conversation = other.conversation(job_id=job_id)
    for action in [
        lambda: pg_store.enqueue_document(
            job_id, "cv", "en", "a", snapshot_id=foreign["snapshot_id"]
        ),
        lambda: pg_store.enqueue_document(
            job_id, "cv", "en", "b", run_id=foreign["search_run"]["id"]
        ),
        lambda: pg_store.enqueue_document(
            job_id, "cv", "en", "c", conversation_id=conversation["id"]
        ),
        lambda: pg_store.enqueue_personal_message(conversation["id"], "forged", "d"),
    ]:
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.status == 404
    assert pg_store.tasks() == []


async def test_parallel_documents_share_gateway_but_not_context_files_facts_or_metadata(
    pg_store, tmp_path
):
    other = personal_store(pg_store.database, "parallel-bob")
    job_id, _ = seed(pg_store, "Alice Example develops backend services.")
    seed(other, "Bob Example designs product interfaces.")
    settings = WebSettings(tmp_path)

    class Gateway(FixtureGateway):
        async def generate(self, task_context):
            await asyncio.sleep(0.01 if task_context["owner_id"] == pg_store.user_id else 0)
            value = await super().generate(task_context)
            return AgentResponse(
                value,
                {
                    "owner_id": task_context["owner_id"],
                    "task_id": task_context["task_id"],
                    "role": "generator",
                    "template_version": task_context["template"]["version"],
                    "session_id": "sess-" + task_context["owner_id"],
                },
            )

    gateway = Gateway()
    tasks = [
        store.enqueue_document(job_id, "cv", "en", "same-request") for store in (pg_store, other)
    ]
    workers = [Worker(store, settings, gateway=gateway) for store in (pg_store, other)]
    assert await asyncio.gather(*(worker.once() for worker in workers)) == [True, True]
    for store, task, name, forbidden in [
        (pg_store, tasks[0], "Alice Example", "Bob Example"),
        (other, tasks[1], "Bob Example", "Alice Example"),
    ]:
        ready = store.task(task["id"])
        assert ready["status"] == "ready", ready["error"]
        assert ready["result"]["generator"]["task_id"] == task["id"]
        assert ready["result"]["generator"]["owner_id"] == store.user_id
        assert ready["result"]["calls"][0]["metadata"]["session_id"] == "sess-" + store.user_id
        for item in ready["artifacts"]:
            artifact = store.artifact(item["id"])
            path = allowed_file(settings, artifact["path"], "artifacts", store.user_id, task["id"])
            assert path.is_relative_to(owner_root(settings, store.user_id))
            text = (
                " ".join(page.extract_text() for page in PdfReader(path).pages)
                if item["format"] == "pdf"
                else " ".join(paragraph.text for paragraph in Document(path).paragraphs)
            )
            assert name in text and forbidden not in text


@pytest.mark.parametrize("key", ["owner_id", "task_id", "template_version", "role"])
async def test_metadata_for_another_task_or_template_blocks_download(pg_store, tmp_path, key):
    job_id, _ = seed(pg_store)
    task = pg_store.enqueue_document(job_id, "cv", "en", "metadata-conflict")

    class Gateway(FixtureGateway):
        async def generate(self, ctx):
            return AgentResponse(await super().generate(ctx), {key: "wrong"})

    assert await Worker(pg_store, WebSettings(tmp_path), gateway=Gateway()).once()
    assert pg_store.task(task["id"])["status"] == "blocked"
    assert pg_store.task(task["id"])["artifacts"] == []


async def test_mutating_gateway_input_cannot_change_confirmed_facts(pg_store, tmp_path):
    job_id, _ = seed(pg_store)
    task = pg_store.enqueue_document(job_id, "cv", "en", "mutating")

    class Gateway(FixtureGateway):
        async def generate(self, ctx):
            ctx["template"]["facts"][0]["text"] = "An invented candidate at Fake Employer."
            return await super().generate(ctx)

        async def review(self, ctx, draft):
            assert ctx["template"]["facts"][0]["text"] == "Alex Example builds Python services."
            return {
                "verdict": "revise",
                "issues": ["Unsupported assertions"],
                "draft_hash": content_hash(draft),
                "checked_fact_ids": [],
            }

    await Worker(pg_store, WebSettings(tmp_path), gateway=Gateway()).once()
    assert pg_store.task(task["id"])["status"] == "blocked"
    assert (
        pg_store.profile()["active"]["facts"][0]["text"] == "Alex Example builds Python services."
    )


def test_private_paths_reject_cross_owner_files_and_parent_symlinks(tmp_path):
    settings = WebSettings(tmp_path)
    first = private_directory(settings, "templates", "alice")
    second = private_directory(settings, "templates", "bob")
    file = second / "cv.pdf"
    file.write_bytes(b"private")
    with pytest.raises(Exception, match="владельцу"):
        allowed_file(settings, file, "templates", "alice")
    link = first / "link"
    link.symlink_to(second, target_is_directory=True)
    with pytest.raises(Exception, match="Ссылки"):
        allowed_file(settings, link / "cv.pdf", "templates", "alice")
    assert oct(first.stat().st_mode & 0o777) == "0o700"
    legacy = tmp_path / "templates"
    legacy.mkdir()
    original = legacy / "old.pdf"
    original.write_bytes(b"legacy")
    assert allowed_file(settings, original, "templates", "alice") == original


@pytest.mark.parametrize(
    "field,value", [("owner_id", None), ("owner_id", ""), ("owner_id", 1), ("task_id", "")]
)
def test_mcp_requires_explicit_identity(field, value):
    data = context()
    data[field] = value
    with pytest.raises(PermissionError):
        scope_context(data)


async def test_mcp_binds_content_and_version_on_every_tool_call():
    data = scope_context(context())
    scope = TaskScope(
        "alice",
        "task-a",
        "version-alice",
        time.time() + 30,
        "generator",
        context_hash=content_hash(data),
        template_id="template-alice",
    )
    server = create_server(data, scope)
    await server.call_tool("get_candidate_facts", {})
    server.context["template"]["facts"][0]["text"] = "different fact"
    with pytest.raises(PermissionError, match="content changed"):
        await server.call_tool("get_candidate_facts", {})


async def test_real_mcp_and_openai_http_contract_are_isolated_for_parallel_users(monkeypatch):
    sessions = {}

    async def provider(request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/agents/sessions":
            body = json.loads(request.content)
            content = json.loads(body["input"][0]["content"][0]["text"])
            fact = content["mcp_snapshot"]["get_candidate_facts"]["facts"][0]["text"]
            actor = fact.split()[0]
            role = (
                "reviewer"
                if "independent factual reviewer" in body["agent"]["instructions"]
                else "generator"
            )
            session_id = "sess_" + actor + role
            sessions[session_id] = (body, fact, role)
            await asyncio.sleep(0.01 if actor == "alice" else 0)
            return httpx.Response(
                200,
                json={
                    "id": session_id,
                    "agent": {**body["agent"], "id": body["agent_id"]},
                    "environment": body["environment"],
                    "vault_ids": body["vault_ids"],
                    "status": "idle",
                },
            )
        session_id = path.split("/")[4]
        body, fact, role = sessions[session_id]
        if path.endswith("/turns"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "turn_" + session_id.removeprefix("sess_"),
                            "session_id": session_id,
                            "status": "completed",
                            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                        }
                    ],
                    "has_more": False,
                },
            )
        if path.endswith("/items"):
            document = {
                "title": "Curriculum Vitae",
                "language": "en",
                "sections": [
                    {
                        "heading": "Experience",
                        "paragraphs": [{"text": fact, "fact_ids": ["same-id"], "kind": "body"}],
                    }
                ],
            }
            result = (
                document
                if role == "generator"
                else {
                    "verdict": "pass",
                    "issues": [],
                    "draft_hash": content_hash(document),
                    "checked_fact_ids": ["same-id"],
                    "assessment": {
                        name: {"passed": True, "issues": []}
                        for name in [
                            "vacancy_alignment",
                            "evidence_selection",
                            "document_structure",
                        ]
                    },
                }
            )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "turn_id": "turn_" + session_id.removeprefix("sess_"),
                            "status": "completed",
                            "content": [{"type": "output_text", "text": json.dumps(result)}],
                        }
                    ],
                    "has_more": False,
                },
            )
        pytest.fail("Unexpected provider route: " + path)

    transport = httpx.MockTransport(provider)
    monkeypatch.setattr(
        "qa_job_bot.agent_gateway.OpenAITransport",
        lambda **kwargs: OpenAITransport(**kwargs, http_transport=transport),
    )
    gateway = AgentGateway(
        OpenAISettings("sk-" + "x" * 24, "agent_synthetic", "gpt-5.6-luna", "medium")
    )
    first, second = await asyncio.gather(
        gateway.generate(context()), gateway.generate(context("bob", "task-b"))
    )
    reviews = await asyncio.gather(
        gateway.review(context(), first), gateway.review(context("bob", "task-b"), second)
    )
    assert first["sections"][0]["paragraphs"][0]["text"].startswith("alice")
    assert second["sections"][0]["paragraphs"][0]["text"].startswith("bob")
    assert first.metadata["owner_id"] == "alice" and second.metadata["task_id"] == "task-b"
    assert len({item.metadata["session_id"] for item in [first, second, *reviews]}) == 4
    assert not hasattr(gateway, "last_run_metadata")
    metadata = first.metadata
    metadata["owner_id"] = "mutated"
    assert first.metadata["owner_id"] == "alice"
    with pytest.raises(AgentUnavailable):
        await gateway.generate({**context(), "owner_id": None})
    assert first.metadata["owner_id"] == "alice"


def docx_bytes(name, skill):
    document = Document()
    document.add_paragraph(name)
    document.add_paragraph(skill)
    result = io.BytesIO()
    document.save(result)
    return result.getvalue()


def test_api_pins_facts_files_historical_card_and_summary_without_exposing_paths(api_world):
    admin, member, app, admin_id, member_id = api_world
    stores = [PostgresStore(app.state.database, uid) for uid in (admin_id, member_id)]
    resources = []
    for client, store, name in [
        (admin, stores[0], "Alice Example"),
        (member, stores[1], "Bob Example"),
    ]:
        uploaded = client.post(
            "/api/template-drafts",
            files={"file": (name + ".docx", docx_bytes(name, name + " develops Python services."))},
        )
        assert uploaded.status_code == 201
        draft = uploaded.json()
        assert "path" not in draft and "image_base64" not in str(draft)
        assert (
            client.post(
                f"/api/template-drafts/{draft['id']}/activate",
                json={"confirmed": True, "expected_version": None},
            ).status_code
            == 200
        )
        current = store.template(draft["id"])
        assert Path(current["path"]).is_relative_to(owner_root(app.state.settings, store.user_id))
        source = client.get(f"/api/templates/{draft['id']}/source")
        assert source.status_code == 200 and source.content == Path(current["path"]).read_bytes()
        raw = vacancy()
        store.upsert_jobs([raw])
        store.publish_search([raw], [])
        listing = client.get("/api/jobs").json()
        queued = client.post(
            f"/api/jobs/{raw.fingerprint}/documents",
            json={
                "kind": "cv",
                "language": "en",
                "snapshot_id": listing["snapshot_id"],
                "request_id": "same-key",
            },
        ).json()
        assert queued["input_summary"]["template_filename"] == name + ".docx"
        assert queued["input_summary"]["job_version"]["snapshot_id"] == listing["snapshot_id"]
        resources.append((draft, queued, listing))
    assert stores[0].profile()["active"]["id"] != stores[1].profile()["active"]["id"]
    for client, store, name in [
        (admin, stores[0], "Alice Example"),
        (member, stores[1], "Bob Example"),
    ]:
        other = member if client is admin else admin
        own = resources[0 if client is admin else 1]
        assert other.get(f"/api/templates/{own[0]['id']}/source").status_code == 404
        asyncio.run(Worker(store, app.state.settings, gateway=FixtureGateway()).once())
        ready = client.get("/api/tasks/" + own[1]["id"]).json()
        assert ready["status"] == "ready", ready.get("error")
        for artifact in ready["artifacts"]:
            assert other.get("/api/artifacts/" + artifact["id"] + "/download").status_code == 404
            downloaded = client.get("/api/artifacts/" + artifact["id"] + "/download")
            assert downloaded.status_code == 200
            if artifact["format"] == "pdf":
                assert name in " ".join(
                    page.extract_text() for page in PdfReader(io.BytesIO(downloaded.content)).pages
                )
        assert client.delete("/api/tasks/" + own[1]["id"]).status_code == 204
        assert (
            client.get("/api/artifacts/" + ready["artifacts"][0]["id"] + "/download").status_code
            == 404
        )
    for body in [{"job_id": vacancy().fingerprint, "snapshot_id": "\ud800"}, {"template_id": "\0"}]:
        assert (
            admin.post(
                "/api/conversations",
                content=json.dumps(body),
                headers={"content-type": "application/json"},
            ).status_code
            == 422
        )


def test_registration_is_atomic_and_refuses_wrong_context_or_late_lease(pg_store, monkeypatch):
    job_id, _ = seed(pg_store)
    task = pg_store.enqueue_document(job_id, "cv", "en", "register")
    claimed = pg_store.claim("worker")
    pg_store.finish_task(task["id"], "worker", "rendering")
    pinned = claimed["payload"]["context"]
    result = {
        "input_hash": content_hash(pinned),
        "template_version": pinned["template"]["version"],
        "job_version": pinned["job_version"],
    }
    with pytest.raises(StoreError, match="другому контексту"):
        pg_store.finish_document(
            task["id"], "worker", {**result, "template_version": "foreign"}, [{"format": "pdf"}]
        )
    with pg_store.read() as connection:
        assert connection.execute(select(s.artifacts)).first() is None
    original = pg_store.live_lease
    calls = []

    def lease(*args):
        calls.append(args)
        return original(*args) if len(calls) == 1 else (*original(*args), s.tasks.c.id == "never")

    monkeypatch.setattr(pg_store, "live_lease", lease)
    with pytest.raises(StoreError, match="Истёк"):
        pg_store.finish_document(task["id"], "worker", result, [{"format": "pdf"}])
    with pg_store.read() as connection:
        assert connection.execute(select(s.artifacts)).first() is None
    monkeypatch.setattr(pg_store, "live_lease", original)
    pg_store.cancel_task(task["id"])
    assert not pg_store.finish_document(task["id"], "worker", result, [])


def test_template_activation_is_independent_across_people(pg_store):
    other = personal_store(pg_store.database, "cv-bob")
    first = template(pg_store)
    second = template(other, "Bob Example speaks German.")
    replacement = template(pg_store, "Alice Example builds developer tools.")
    assert pg_store.profile()["active"]["id"] == replacement["id"] != first["id"]
    assert other.profile()["active"]["id"] == second["id"]
    with pytest.raises(StoreError) as error:
        other.activate_template(first["id"], second["id"])
    assert error.value.status == 404


def test_gateway_result_metadata_copies_do_not_mutate_transport_or_other_results():
    source = {"session_id": "sess_private", "usage": {"input_tokens": 3}}
    first = AgentResponse({"reply": "one"}, source)
    second = AgentResponse({"reply": "two"}, source)
    source["usage"]["input_tokens"] = 100
    leaked = first.metadata
    leaked["usage"]["input_tokens"] = 500
    assert first.metadata["usage"]["input_tokens"] == second.metadata["usage"]["input_tokens"] == 3


def test_migration_preserves_legacy_conversation_and_adopts_its_messages(pg_store):
    job_id, _ = seed(pg_store)
    conversation = pg_store.conversation(job_id=job_id)
    pg_store.add_message(conversation["id"], "user", "Preserved legacy preference", "legacy")
    with pg_store.write() as connection:
        connection.execute(
            s.conversations.update()
            .where(s.conversations.c.id == conversation["id"])
            .values(scope=f"job:{job_id}")
        )
    with pg_store.database.engine.begin() as connection:
        command.downgrade(pg_store.database.migration_config(connection), "0005_personal_runs")
    assert "payload" not in {
        column["name"] for column in inspect(pg_store.database.engine).get_columns("conversations")
    }
    pg_store.database.migrate()
    assert pg_store.conversation(job_id=job_id)["id"] == conversation["id"]
    assert pg_store.messages(conversation["id"])[0]["text"] == "Preserved legacy preference"
    task = pg_store.enqueue_document(job_id, "cv", "en", "after-upgrade")
    context = pg_store.task(task["id"], internal=True)["payload"]["context"]
    assert context["owner_messages"] == ["Preserved legacy preference"]
    assert context["job"]["description"] == vacancy().description


def test_source_file_check_rejects_foreign_pointer_even_when_content_hash_matches(api_world):
    admin, member, app, admin_id, member_id = api_world
    stores = [PostgresStore(app.state.database, uid) for uid in (admin_id, member_id)]
    templates = []
    for client, name in [(admin, "Alice Example"), (member, "Bob Example")]:
        response = client.post(
            "/api/template-drafts", files={"file": ("cv.docx", docx_bytes(name, "Confirmed work."))}
        )
        assert response.status_code == 201
        templates.append(response.json())
    foreign = stores[1].template(templates[1]["id"])
    with stores[0].write() as connection:
        original = stores[0].row(connection, s.templates, templates[0]["id"], "Missing")
        payload = {
            **original["payload"],
            "path": foreign["path"],
            "source_sha256": foreign["source_sha256"],
        }
        connection.execute(
            s.templates.update()
            .where(s.templates.c.id == templates[0]["id"])
            .values(payload=payload)
        )
    assert admin.get(f"/api/templates/{templates[0]['id']}/source").status_code == 404
    assert member.get(f"/api/templates/{templates[1]['id']}/source").status_code == 200


def test_private_paths_refuse_traversal_and_unavailable_files(tmp_path):
    settings = WebSettings(tmp_path)
    own = private_directory(settings, "templates", "alice")
    escaped = own / ".." / ".." / ".." / "outside.docx"
    with pytest.raises(DocumentFileError):
        allowed_file(settings, escaped, "templates", "alice")
    with pytest.raises(DocumentFileError, match="недоступен"):
        allowed_file(settings, own / "missing.docx", "templates", "alice")
    for invalid in ["..", "../foreign", "nested/file", ""]:
        with pytest.raises(DocumentFileError):
            private_directory(settings, "artifacts", "alice", invalid)


def test_generation_without_active_template_rolls_back_conversation_and_task(pg_store):
    raw = vacancy()
    pg_store.upsert_jobs([raw])
    pg_store.publish_search([raw], [])
    with pytest.raises(StoreError) as error:
        pg_store.enqueue_document(raw.fingerprint, "cv", "en", "missing-template")
    assert error.value.status == 409
    with pg_store.read() as connection:
        assert connection.execute(select(s.conversations)).first() is None
        assert connection.execute(select(s.tasks)).first() is None


def test_equal_job_content_reuses_discussion_but_pins_the_explicit_search_origin(pg_store):
    job_id, _ = seed(pg_store)
    first = pg_store.list_jobs("active", 1)
    discussion = pg_store.conversation(job_id=job_id, snapshot_id=first["snapshot_id"])
    pg_store.add_message(discussion["id"], "user", "Keep my preference", "preference")
    pg_store.publish_search([vacancy()], [])
    second = pg_store.list_jobs("active", 1)
    assert first["search_run"]["id"] != second["search_run"]["id"]
    assert (
        pg_store.conversation(job_id=job_id, snapshot_id=second["snapshot_id"])["id"]
        == discussion["id"]
    )
    for with_conversation in [False, True]:
        queued = pg_store.enqueue_document(
            job_id,
            "cv",
            "en",
            f"same-content-{with_conversation}",
            snapshot_id=second["snapshot_id"],
            conversation_id=discussion["id"] if with_conversation else None,
        )
        context = pg_store.task(queued["id"], internal=True)["payload"]["context"]
        assert context["job_version"]["snapshot_id"] == second["snapshot_id"]
        assert context["job_version"]["run_id"] == second["search_run"]["id"]
        assert context["owner_messages"] == ["Keep my preference"]
