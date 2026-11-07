from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from qa_job_bot.models import Job
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings


@pytest.fixture
def client(tmp_path):
    app = create_app(WebSettings(data_dir=tmp_path, origins=("http://testserver",)))
    with TestClient(app, headers={"origin": "http://testserver"}) as current:
        setup = current.post(
            "/api/setup",
            json={
                "token": app.state.security.setup_file.read_text(),
                "password": "long-test-password",
            },
        )
        assert setup.status_code == 200
        current.headers["x-csrf-token"] = setup.json()["csrf_token"]
        yield current


def candidate(store, expected=None):
    draft = store.add_template(
        {"facts": [{"id": "fact-1", "text": "I test software", "source": "User confirmation"}]}
    )
    return store.activate_template(draft["id"], expected)


def vacancy(store):
    job = Job(
        "fixture",
        "1",
        "QA Engineer",
        "Acme",
        "Europe",
        "https://jobs.example/1",
        description="Requirements\nPython and English B2",
    )
    store.upsert_jobs([job])
    store.publish_search([job], [])
    return job


def test_completed_chat_replay_uses_original_task_after_context_changes(client):
    store = client.app.state.store
    first_profile = candidate(store)
    job = vacancy(store)
    conversation = store.conversation(job_id=job.fingerprint)["id"]
    route = f"/api/conversations/{conversation}/messages"
    request = {"text": "Original user question", "request_id": "first"}
    first = client.post(route, json=request)
    assert first.status_code == 202
    task_id = first.json()["id"]
    store.claim("worker")
    store.finish_chat(task_id, "worker", "Original answer")
    second = client.post(route, json={"text": "Next user question", "request_id": "second"})
    assert second.status_code == 202
    store.claim("worker")
    store.finish_chat(second.json()["id"], "worker", "Second answer")
    candidate(store, first_profile["id"])
    store.upsert_jobs([replace(job, description="Changed job description")])
    replay = client.post(route, json=request)
    assert replay.status_code == 202
    assert replay.json()["id"] == task_id
    assert replay.json()["status"] == "ready"
    assert len(store.messages(conversation)) == 4
    assert len(store.tasks()) == 2


def test_chat_key_conflicts_do_not_save_orphan_messages(client):
    store = client.app.state.store
    template = candidate(store)
    first_conversation = template["conversation_id"]
    another_template = store.add_template({"facts": []})
    second_conversation = another_template["conversation_id"]
    route = f"/api/conversations/{first_conversation}/messages"
    request = {"text": "Original", "request_id": "same-key"}
    assert client.post(route, json=request).status_code == 202
    conflicting_route = f"/api/conversations/{second_conversation}/messages"
    assert client.post(conflicting_route, json=request).status_code == 409
    assert store.messages(second_conversation) == []
    assert client.post(route, json={**request, "text": "Changed"}).status_code == 409
    assert len(store.messages(first_conversation)) == 1
    assert (
        client.post(route, json={"text": "Concurrent", "request_id": "different"}).status_code
        == 409
    )
    assert len(store.messages(first_conversation)) == 1


def test_document_request_aliases_survive_completion_profile_and_source_changes(client):
    store = client.app.state.store
    profile = candidate(store)
    job = vacancy(store)
    route = f"/api/jobs/{job.fingerprint}/documents"
    first_request = {"kind": "cv", "language": "en", "request_id": "first"}
    first = client.post(route, json=first_request)
    alias_request = {**first_request, "request_id": "alias"}
    alias = client.post(route, json=alias_request)
    assert first.status_code == alias.status_code == 202
    assert first.json()["id"] == alias.json()["id"]
    store.claim("worker")
    store.finish_task(first.json()["id"], "worker", "ready")
    replacement = candidate(store, profile["id"])
    store.upsert_jobs([replace(job, description="New confirmed job description")])
    for request in (first_request, alias_request):
        repeated = client.post(route, json=request)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == first.json()["id"]
    new = client.post(route, json={**first_request, "request_id": "new"})
    assert new.status_code == 202
    assert new.json()["id"] != first.json()["id"]
    context = store.task(new.json()["id"], internal=True)["payload"]["context"]
    assert context["template"]["id"] == replacement["id"]
    assert context["job"]["description"] == "New confirmed job description"
    conflict = client.post(
        route, json={"kind": "cover_letter", "language": "auto", "request_id": "first"}
    )
    assert conflict.status_code == 409


def test_new_generation_after_source_change_gets_new_snapshot_while_previous_runs(client):
    store = client.app.state.store
    candidate(store)
    job = vacancy(store)
    route = f"/api/jobs/{job.fingerprint}/documents"
    first = client.post(route, json={"kind": "cv", "language": "en", "request_id": "first"})
    store.upsert_jobs([replace(job, description="Updated responsibilities")])
    second = client.post(route, json={"kind": "cv", "language": "en", "request_id": "second"})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert len(store.tasks()) == 2


def test_generation_uses_user_preferences_without_promoting_them_to_facts(client):
    store = client.app.state.store
    profile = candidate(store)
    job = vacancy(store)
    route = f"/api/jobs/{job.fingerprint}/documents"
    original_request = {"kind": "cv", "language": "en", "request_id": "original"}
    original = client.post(route, json=original_request).json()
    store.claim("worker")
    store.finish_task(original["id"], "worker", "ready")
    conversation = store.conversation(job_id=job.fingerprint)["id"]
    store.add_message(
        conversation, "user", "Focus on API testing and keep it concise", "preference"
    )
    store.add_message(conversation, "assistant", "I will use that preference", "preference")
    updated = client.post(route, json={**original_request, "request_id": "updated"}).json()
    current_context = store.task(updated["id"], internal=True)["payload"]["context"]
    assert current_context["owner_messages"] == ["Focus on API testing and keep it concise"]
    assert current_context["conversation_messages"] == [
        {"role": "user", "text": "Focus on API testing and keep it concise"},
        {"role": "assistant", "text": "I will use that preference"},
    ]
    assert current_context["template"]["facts"] == profile["facts"]
    old_context = store.task(original["id"], internal=True)["payload"]["context"]
    assert old_context["owner_messages"] == []
    assert old_context["conversation_messages"] == []
    replay = client.post(route, json=original_request)
    assert replay.status_code == 202
    assert replay.json()["id"] == original["id"]


def test_restore_keeps_job_visible_after_search_no_longer_returns_it(client):
    store = client.app.state.store
    job = vacancy(store)
    state_route = f"/api/jobs/{job.fingerprint}/state"
    assert client.patch(state_route, json={"archived": True, "applied": True}).status_code == 200
    store.publish_search([], [])
    assert client.get("/api/jobs?tab=archive").json()["total"] == 1
    assert client.patch(state_route, json={"archived": False}).status_code == 200
    store.publish_search([], [])
    active = client.get("/api/jobs").json()
    assert active["total"] == 0
    applied = client.get("/api/jobs?tab=applied").json()
    assert applied["total"] == 1
    assert applied["items"][0]["id"] == job.fingerprint
    assert applied["items"][0]["applied_at"]


def test_unicode_setup_token_and_oversized_numeric_length_fail_without_server_error(tmp_path):
    app = create_app(WebSettings(data_dir=tmp_path, origins=("http://testserver",)))
    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        result = client.post(
            "/api/setup", json={"token": "неверный код", "password": "long-password"}
        )
        assert result.status_code == 403
        result = client.post("/api/login", content=b"{}", headers={"content-length": "9" * 5000})
        assert result.status_code == 413
