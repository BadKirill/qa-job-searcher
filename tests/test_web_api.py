import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from docx import Document
from fastapi.testclient import TestClient

from qa_job_bot import web
from qa_job_bot.document_policy import content_hash
from qa_job_bot.models import Job
from qa_job_bot.web import COOKIE, MAX_BODY, changes, create_app
from qa_job_bot.web_security import WebSettings


@pytest.fixture
def client(tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<h1>Application</h1>")
    app = create_app(WebSettings(tmp_path / "data", ("http://testserver",), static))
    with TestClient(app, headers={"origin": "http://testserver"}) as current:
        yield current


def authenticate(client):
    token = client.app.state.security.setup_file.read_text()
    response = client.post("/api/setup", json={"token": token, "password": "test-password-long"})
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return client.app.state.store


def job(store, index=0):
    data = Job(
        "fixture",
        str(index),
        f"Senior QA {index}",
        "Example",
        "Remote",
        "https://example.com",
        description="Looking for a senior quality engineer to automate our testing in Python.",
    )
    store.upsert_jobs([data])
    return data.fingerprint


def test_job_response_preserves_score_reasons_and_search_freshness(client):
    store = authenticate(client)
    listing = Job(
        "fixture",
        "score-1",
        "Senior QA",
        "Example",
        "Remote Europe",
        "https://example.com/score-1",
        score=87,
        match_reasons=["QA role", "Remote Europe"],
    )
    store.upsert_jobs([listing])
    first = client.get("/api/jobs").json()
    assert first["last_search_at"] is None
    assert first["snapshot_at"]
    assert first["items"][0]["score"] == 87
    assert first["items"][0]["match_reasons"] == ["QA role", "Remote Europe"]
    assert first["items"][0]["last_seen_at"]
    store.publish_search([listing], [])
    latest = client.get("/api/jobs").json()
    assert latest["last_search_at"]
    assert latest["snapshot_at"]
    assert (
        client.get(f"/api/jobs?tab=active&page=1&snapshot_id={first['snapshot_id']}").json()[
            "snapshot_at"
        ]
        == first["snapshot_at"]
    )


def template_file():
    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph("Alex Example")
    document.add_paragraph("QA Engineer with Python experience since 2020.")
    document.save(buffer)
    return buffer.getvalue()


def upload(client):
    response = client.post(
        "/api/template-drafts", files={"file": ("candidate.docx", template_file())}
    )
    assert response.status_code == 201, response.text
    return response.json()


def activate(client, template, expected=None):
    return client.post(
        f"/api/template-drafts/{template['id']}/activate",
        json={
            "expected_version": expected,
            "confirmed": True,
        },
    )


def test_session_auth_security_headers_and_logout(client):
    assert client.get("/api/session").json() == {
        "authenticated": False,
        "setup_required": True,
        "csrf_token": None,
    }
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert (
        client.post(
            "/api/login", json={"password": "wrong"}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post("/api/setup", json={"token": "wrong", "password": "too-short"}).status_code
        == 422
    )
    assert (
        client.post("/api/setup", json={"token": "wrong", "password": "long-password"}).status_code
        == 403
    )
    store = authenticate(client)
    assert store.get_setting("password") != "test-password-long"
    response = client.get("/api/session")
    assert response.json()["authenticated"]
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.post("/api/search-runs", headers={"x-csrf-token": ""}).status_code == 403
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"password": "test-password-long"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/profile").json() == {"active": None, "drafts": []}


def test_secure_cookie_and_request_size(tmp_path):
    app = create_app(WebSettings(tmp_path, ("https://testserver",)))
    with TestClient(
        app, base_url="https://testserver", headers={"origin": "https://testserver"}
    ) as client:
        response = client.post(
            "/api/setup",
            json={"token": app.state.security.setup_file.read_text(), "password": "long-password"},
        )
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/session").json()["authenticated"]
        for length in (str(MAX_BODY + 1), "-1", "bad"):
            assert (
                client.post(
                    "/api/login", content=b"x", headers={"content-length": length}
                ).status_code
                == 413
            )
        assert (
            client.post(
                "/api/login", content=b"x" * (MAX_BODY + 1), headers={"content-length": "0"}
            ).status_code
            == 413
        )


def test_jobs_pagination_state_snapshot_and_tasks(client):
    store = authenticate(client)
    ids = [job(store, index) for index in range(21)]
    page = client.get("/api/jobs").json()
    assert len(page["items"]) == 10 and page["pages"] == 3
    assert (
        len(
            client.get("/api/jobs", params={"page": 3, "snapshot_id": page["snapshot_id"]}).json()[
                "items"
            ]
        )
        == 1
    )
    for params in (
        {"page": 0},
        {"page": "bad"},
        {"tab": "bad"},
        {"page_size": 11},
        {"page_size": 0},
        {"page_size": "bad"},
    ):
        assert client.get("/api/jobs", params=params).status_code == 422
    assert client.get("/api/jobs?snapshot_id=missing").status_code == 409
    assert client.patch(f"/api/jobs/{ids[0]}/state", json={}).status_code == 422
    assert client.patch(f"/api/jobs/{ids[0]}/state", json={"applied": "true"}).status_code == 422
    response = client.patch(f"/api/jobs/{ids[0]}/state", json={"applied": True, "archived": True})
    assert response.json()["applied_at"] and response.json()["archived_at"]
    assert client.get("/api/jobs?tab=archive").json()["total"] == 1
    assert client.patch(f"/api/jobs/{ids[0]}/state", json={"archived": False}).json()["applied_at"]
    assert client.get("/api/jobs").json()["total"] == 20
    assert client.get("/api/jobs?tab=applied").json()["total"] == 1
    assert client.patch("/api/jobs/missing/state", json={"applied": True}).status_code == 404
    task = client.post("/api/search-runs", json={}).json()
    assert client.post("/api/search-runs", json={}).json()["id"] == task["id"]
    assert client.get("/api/tasks").json()["items"][0]["id"] == task["id"]
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "queued"
    assert client.get(f"/api/tasks/{task['id']}/review").json() == {}
    assert client.post(f"/api/tasks/{task['id']}/cancel").json()["status"] == "cancelled"
    assert client.get("/api/tasks/missing").status_code == 404


def test_upload_activation_validation_and_version_conflict(client):
    authenticate(client)
    for filename, content in (("bad.txt", b"text"), ("bad.docx", b"corrupt"), ("empty.pdf", b"")):
        assert (
            client.post("/api/template-drafts", files={"file": (filename, content)}).status_code
            == 422
        )
    assert (
        client.post(
            "/api/template-drafts", files={"file": ("large.docx", b"x" * (10 * 1024 * 1024 + 1))}
        ).status_code
        == 413
    )
    first = upload(client)
    assert "path" not in first and "style" not in first and len(first["facts"]) >= 2
    route = f"/api/template-drafts/{first['id']}"
    for facts in ([], [first["facts"][0]] * 2, [{"id": "f", "text": " ", "source": "owner"}]):
        assert client.patch(route, json={"facts": facts}).status_code == 422
    assert (
        client.patch(route, json={"facts": first["facts"], "status": "active"}).status_code == 422
    )
    assert (
        client.patch(
            route, json={"facts": first["facts"], "instructions": "Keep concise"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            route + "/activate", json={"expected_version": None, "confirmed": False}
        ).status_code
        == 422
    )
    assert activate(client, first).status_code == 200
    second = upload(client)
    assert activate(client, second).status_code == 409
    assert client.get("/api/profile").json()["active"]["id"] == first["id"]
    assert activate(client, second, first["version"]).status_code == 200
    assert client.patch(route, json={"facts": first["facts"]}).status_code == 409


def test_document_enqueue_language_idempotency_and_snapshots(client):
    store = authenticate(client)
    key = job(store)
    route = f"/api/jobs/{key}/documents"
    payload = {"kind": "cv", "language": "en", "request_id": "r1"}
    assert client.post(route, json=payload).status_code == 409
    current = upload(client)
    activate(client, current)
    assert client.post(route, json={**payload, "language": "auto"}).status_code == 422
    assert client.post(route, json={**payload, "kind": "cover_letter"}).status_code == 422
    first = client.post(route, json=payload)
    assert first.status_code == 202
    assert client.post(route, json=payload).json()["id"] == first.json()["id"]
    assert (
        client.post(route, json={**payload, "request_id": "r2"}).json()["id"] == first.json()["id"]
    )
    assert "context" not in first.json()
    assert (
        client.post(
            route, json={"kind": "cover_letter", "language": "auto", "request_id": "cover"}
        ).status_code
        == 202
    )
    assert (
        store.task(first.json()["id"], internal=True)["payload"]["context"]["template"]["version"]
        == current["version"]
    )


def test_web_chat_guards_persistence_and_job_scope(client):
    store = authenticate(client)
    key = job(store)
    assert client.post("/api/conversations", json={}).status_code == 422
    conversation = client.post("/api/conversations", json={"job_id": key}).json()
    route = f"/api/conversations/{conversation['id']}/messages"
    assert client.post(route, json={"text": "hello", "request_id": "before"}).status_code == 409
    current = upload(client)
    activate(client, current)
    for text in ("", " ", "x" * 6001):
        assert client.post(route, json={"text": text, "request_id": "bad"}).status_code == 422
    payload = {"text": "Use a concise format", "request_id": "msg1"}
    first = client.post(route, json=payload)
    assert first.status_code == 202
    assert client.post(route, json=payload).json()["id"] == first.json()["id"]
    assert client.post(route, json={**payload, "request_id": "msg2"}).status_code == 409
    assert len(client.get(route).json()["items"]) == 1
    draft = upload(client)
    scope = client.post("/api/conversations", json={"template_id": draft["id"]}).json()
    assert scope["id"] == draft["conversation_id"]
    assert (
        client.post(
            f"/api/conversations/{scope['id']}/messages",
            json={"text": "Help", "request_id": "draftmsg"},
        ).status_code
        == 202
    )


def approved_artifact(client, path=None):
    store = client.app.state.store
    task = store.enqueue("cv", "test", {}, "artifact-task")
    store.claim("worker")
    target = path or client.app.state.settings.data_dir / "artifacts" / task["id"] / "cv.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-fixture")
    artifact = store.add_artifact(
        task["id"],
        {
            "filename": "cv.pdf",
            "format": "pdf",
            "path": str(target),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "draft_hash": "a" * 64,
        },
    )
    store.finish_task(
        task["id"],
        "worker",
        "ready",
        result={
            "verified": True,
            "draft_hash": "a" * 64,
            "artifact_ids": [artifact["id"]],
        },
    )
    return artifact, target


def test_download_gate_integrity_and_static_security(client, tmp_path):
    authenticate(client)
    assert client.get("/").status_code == 200
    assert client.get("/missing").status_code == 404
    assert client.get("/api/artifacts/missing/download").status_code == 404
    artifact, path = approved_artifact(client)
    route = f"/api/artifacts/{artifact['id']}/download"
    response = client.get(route)
    assert response.status_code == 200 and response.content == b"%PDF-fixture"
    assert "attachment" in response.headers["content-disposition"]
    preview = client.get(f"/api/artifacts/{artifact['id']}/preview")
    assert preview.status_code == 200 and preview.content == b"%PDF-fixture"
    assert "inline" in preview.headers["content-disposition"]
    path.write_bytes(b"tampered")
    assert client.get(route).status_code == 409
    assert client.get(f"/api/artifacts/{artifact['id']}/preview").status_code == 409
    path.unlink()
    assert client.get(route).status_code == 404
    store = client.app.state.store
    with store._connect() as db:
        db.execute("UPDATE web_tasks SET status='blocked' WHERE id=?", (artifact["task_id"],))
    assert client.get(route).status_code == 409
    outside = tmp_path / "outside-secret"
    outside.write_text("not public")
    (client.app.state.settings.frontend_dir / "escape").symlink_to(outside)
    assert client.get("/escape").status_code == 404


def test_document_preview_is_private_hash_bound_and_uses_pinned_facts(client):
    store = client.app.state.store
    draft = {
        "title": "Alex Example",
        "language": "en",
        "sections": [
            {
                "heading": "Experience",
                "paragraphs": [{"text": "API testing at Acme", "fact_ids": ["f1"], "kind": "body"}],
            }
        ],
    }
    payload = {
        "context": {
            "template": {
                "facts": [
                    {"id": "f1", "text": "API testing at Acme", "source": "paragraph:1"},
                    {"id": "f2", "text": "Unused", "source": "paragraph:2"},
                ]
            }
        }
    }
    task = store.enqueue("cv", "preview", payload, "preview-request")
    assert client.get(f"/api/tasks/{task['id']}/document-preview").status_code == 401
    authenticate(client)
    route = f"/api/tasks/{task['id']}/document-preview"
    assert client.get(route).status_code == 404
    store.claim("worker")
    store.finish_task(
        task["id"],
        "worker",
        "ready",
        result={
            "verified": True,
            "draft_hash": content_hash(draft),
            "approved_draft": draft,
        },
    )
    result = client.get(route)
    assert result.status_code == 200
    assert result.json()["draft"] == draft
    assert result.json()["facts"] == [payload["context"]["template"]["facts"][0]]
    assert "approved_draft" not in client.get(f"/api/tasks/{task['id']}").json()["result"]
    assert client.get(f"/api/tasks/{task['id']}").json()["preview_available"]
    with store._connect() as db:
        missing_fact = {"context": {"template": {"facts": []}}}
        db.execute(
            "UPDATE web_tasks SET payload=? WHERE id=?",
            (json.dumps(missing_fact), task["id"]),
        )
    assert client.get(route).status_code == 409
    with store._connect() as db:
        db.execute(
            "UPDATE web_tasks SET payload=? WHERE id=?",
            (json.dumps({}), task["id"]),
        )
    assert client.get(route).status_code == 409
    with store._connect() as db:
        db.execute(
            "UPDATE web_tasks SET payload=? WHERE id=?",
            (json.dumps(payload), task["id"]),
        )
    for status, stored, expected in [
        (
            "ready",
            {"verified": False, "draft_hash": content_hash(draft), "approved_draft": draft},
            404,
        ),
        ("ready", {"verified": True, "draft_hash": content_hash(draft)}, 404),
        (
            "blocked",
            {"verified": True, "draft_hash": content_hash(draft), "approved_draft": draft},
            404,
        ),
    ]:
        with store._connect() as db:
            db.execute(
                "UPDATE web_tasks SET status=?,result=? WHERE id=?",
                (status, json.dumps(stored), task["id"]),
            )
        assert client.get(route).status_code == expected
        assert not client.get(f"/api/tasks/{task['id']}").json()["preview_available"]
    with store._connect() as db:
        broken = {"verified": True, "draft_hash": "0" * 64, "approved_draft": draft}
        db.execute(
            "UPDATE web_tasks SET status='ready',result=? WHERE id=?",
            (json.dumps(broken), task["id"]),
        )
    assert client.get(route).status_code == 409


def test_inline_preview_rejects_docx_and_unregistered_artifact(client):
    authenticate(client)
    artifact, path = approved_artifact(client)
    store = client.app.state.store
    docx_path = path.with_suffix(".docx")
    docx_path.write_bytes(b"synthetic docx")
    docx = store.add_artifact(
        artifact["task_id"],
        {
            "filename": "cv.docx",
            "format": "docx",
            "path": str(docx_path),
            "sha256": hashlib.sha256(docx_path.read_bytes()).hexdigest(),
            "draft_hash": artifact["draft_hash"],
        },
    )
    route = f"/api/artifacts/{docx['id']}/preview"
    assert client.get(route).status_code == 409
    with store._connect() as db:
        result = store.task(artifact["task_id"], internal=True)["result"]
        result["artifact_ids"].append(docx["id"])
        db.execute(
            "UPDATE web_tasks SET result=? WHERE id=?",
            (json.dumps(result), artifact["task_id"]),
        )
    assert client.get(route).status_code == 422


def test_document_deletion_removes_registered_and_broken_files(client):
    store = authenticate(client)
    artifact, path = approved_artifact(client)
    task_id = artifact["task_id"]
    unregistered = path.parent / "broken-preview.png"
    unregistered.write_bytes(b"broken")

    response = client.delete(f"/api/tasks/{task_id}")

    assert response.status_code == 204
    assert not path.parent.exists()
    assert client.get(f"/api/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/artifacts/{artifact['id']}/download").status_code == 404
    assert all(item["id"] != task_id for item in client.get("/api/tasks").json()["items"])
    assert store.task(task_id, internal=True)["payload"] == {}


def test_document_deletion_rejects_active_search_and_unsafe_directory(client, tmp_path):
    store = authenticate(client)
    active = store.enqueue("cv", "active", {}, "active-request")
    assert client.delete(f"/api/tasks/{active['id']}").status_code == 409
    store.cancel_task(active["id"])
    assert client.delete(f"/api/tasks/{active['id']}").status_code == 204
    search = store.enqueue("search", "search", {}, "search-request")
    store.cancel_task(search["id"])
    assert client.delete(f"/api/tasks/{search['id']}").status_code == 422

    broken = store.enqueue("cv", "broken", {}, "broken-request")
    store.cancel_task(broken["id"])
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    directory = client.app.state.settings.data_dir / "artifacts" / broken["id"]
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(outside, target_is_directory=True)
    assert client.delete(f"/api/tasks/{broken['id']}").status_code == 409
    assert outside.exists()
    assert store.task(broken["id"])["status"] == "cancelled"
    directory.unlink()
    directory.write_bytes(b"not-a-directory")
    assert client.delete(f"/api/tasks/{broken['id']}").status_code == 409
    assert store.task(broken["id"])["status"] == "cancelled"


def test_document_deletion_restores_directory_after_cleanup_failure(client, monkeypatch):
    authenticate(client)
    artifact, path = approved_artifact(client)
    original_rmtree = web.shutil.rmtree

    def fail_cleanup(_path):
        raise OSError("read only")

    monkeypatch.setattr(web.shutil, "rmtree", fail_cleanup)
    response = client.delete(f"/api/tasks/{artifact['task_id']}")

    assert response.status_code == 500
    assert path.exists()
    assert client.get(f"/api/tasks/{artifact['task_id']}").status_code == 200
    monkeypatch.setattr(web.shutil, "rmtree", original_rmtree)
    assert client.delete(f"/api/tasks/{artifact['task_id']}").status_code == 204


async def test_sse_change_heartbeat_and_revocation(client, monkeypatch):
    store = authenticate(client)
    token = client.cookies[COOKIE]
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr(web.asyncio, "sleep", AsyncMock())
    stream = changes(request, client.app.state.security, token)
    assert "event: change" in await anext(stream)
    assert "heartbeat" in await anext(stream)
    store.set_setting("revision", 10)
    assert json.loads((await anext(stream)).split("data: ")[1]) == {"revision": 11}
    client.app.state.security.logout(token)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    request.is_disconnected.return_value = True
    with pytest.raises(StopAsyncIteration):
        await anext(changes(request, client.app.state.security, token))


def test_main_invokes_loopback_server(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "create_app", lambda: "app")
    monkeypatch.setattr(web.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    web.main()
    assert calls[0][1]["host"] == "127.0.0.1" and not calls[0][1]["proxy_headers"]
