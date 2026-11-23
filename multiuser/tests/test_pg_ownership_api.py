import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from pg_fixtures import personal_store

from qa_job_bot.models import Job
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings


def sign_in(client):
    response = client.post(
        "/api/setup",
        json={
            "token": client.app.state.security.setup_file.read_text(),
            "password": "synthetic-owner-password",
        },
    )
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


def test_actual_api_isolates_all_storage_objects_between_owners(pg_store, tmp_path):
    a_store = pg_store
    b_store = personal_store(a_store.database, "api-other")
    settings_a = WebSettings(tmp_path / "a", ("http://testserver",))
    settings_b = WebSettings(tmp_path / "b", ("http://testserver",))
    with (
        TestClient(create_app(settings_a, a_store), headers={"origin": "http://testserver"}) as a,
        TestClient(create_app(settings_b, b_store), headers={"origin": "http://testserver"}) as b,
    ):
        sign_in(a)
        sign_in(b)
        assert a.app.state.security.session(a.cookies["qa_session"])["user_id"] == a_store.user_id
        assert b.app.state.security.session(b.cookies["qa_session"])["user_id"] == b_store.user_id
        assert a.app.state.security.session(b.cookies["qa_session"]) is None
        vacancy = Job(
            "Synthetic private fixture",
            "b",
            "Private B vacancy",
            "Synthetic B",
            "Europe",
            "https://example.com/only-b",
            description="Only B can see this job.",
            score=70,
        )
        b_store.upsert_jobs([vacancy])
        b_store.publish_search([vacancy], [])
        assert a.get("/api/jobs").json()["total"] == 0
        snapshot = b.get("/api/jobs").json()["snapshot_id"]
        pdf = Path(__file__).parent / "fixtures" / "candidate.pdf"
        uploaded = b.post(
            "/api/template-drafts", files={"file": ("candidate.pdf", pdf.read_bytes())}
        )
        assert uploaded.status_code == 201
        template_id = uploaded.json()["id"]
        assert b.get(f"/api/templates/{template_id}/source").status_code == 200
        assert b.get(f"/api/templates/{template_id}/pages/1").status_code == 200
        active = b.post(
            f"/api/template-drafts/{template_id}/activate",
            json={"confirmed": True, "expected_version": None},
        )
        assert active.status_code == 200
        conversation = b_store.conversation(job_id=vacancy.fingerprint)
        b_store.add_message(conversation["id"], "user", "B private question", "b-message")
        assert b.get(f"/api/conversations/{conversation['id']}/messages").json()["items"]
        task = b_store.enqueue(
            "cv",
            "private-document",
            {
                "job_id": vacancy.fingerprint,
                "context": {
                    "template": b_store.template(template_id),
                    "job": b_store.get_job(vacancy.fingerprint),
                },
            },
            "private-document",
        )
        b_store.claim("worker-b")
        path = settings_b.data_dir / "artifacts" / task["id"] / "synthetic.pdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"%PDF-synthetic-only-B")
        artifact = b_store.add_artifact(
            task["id"],
            {
                "filename": "synthetic.pdf",
                "format": "pdf",
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "draft_hash": "a" * 64,
            },
        )
        b_store.finish_task(
            task["id"],
            "worker-b",
            "ready",
            result={"verified": True, "draft_hash": "a" * 64, "artifact_ids": [artifact["id"]]},
        )
        assert b.get(f"/api/artifacts/{artifact['id']}/download").status_code == 200
        assert b.get(f"/api/tasks/{task['id']}").status_code == 200
        assert a.get("/api/profile").json() == {"active": None, "drafts": []}
        assert a.get("/api/tasks").json()["items"] == []
        for route in [
            f"/api/jobs?snapshot_id={snapshot}",
            f"/api/tasks/{task['id']}",
            f"/api/tasks/{task['id']}/review",
            f"/api/tasks/{task['id']}/document-preview",
            f"/api/templates/{template_id}/source",
            f"/api/templates/{template_id}/pages/1",
            f"/api/conversations/{conversation['id']}/messages",
            f"/api/artifacts/{artifact['id']}/download",
            f"/api/artifacts/{artifact['id']}/preview",
        ]:
            assert a.get(route).status_code == 404, route
        mutations = [
            ("PATCH", f"/api/jobs/{vacancy.fingerprint}/state", {"applied": True}),
            ("POST", f"/api/templates/{template_id}/reparse", {}),
            (
                "PATCH",
                f"/api/template-drafts/{template_id}",
                {"facts": [{"id": "f1", "text": "forged", "source": "forged"}]},
            ),
            (
                "POST",
                f"/api/template-drafts/{template_id}/activate",
                {"confirmed": True, "expected_version": None},
            ),
            ("POST", "/api/conversations", {"template_id": template_id}),
            ("POST", "/api/conversations", {"job_id": vacancy.fingerprint}),
            (
                "POST",
                f"/api/conversations/{conversation['id']}/messages",
                {"text": "forged", "request_id": "foreign-chat"},
            ),
            ("POST", f"/api/tasks/{task['id']}/cancel", {}),
            ("DELETE", f"/api/tasks/{task['id']}", None),
            (
                "POST",
                f"/api/jobs/{vacancy.fingerprint}/documents",
                {"kind": "cv", "language": "en", "request_id": "foreign-cv"},
            ),
        ]
        for method, route, payload in mutations:
            assert a.request(method, route, json=payload).status_code == 404, route
        assert path.read_bytes() == b"%PDF-synthetic-only-B"
        assert b_store.get_job(vacancy.fingerprint)["applied_at"] is None
        assert len(b_store.messages(conversation["id"])) == 1
        assert b_store.task(task["id"])["status"] == "ready"
