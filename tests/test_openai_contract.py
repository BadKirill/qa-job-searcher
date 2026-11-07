import base64
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image

from qa_job_bot import agent_gateway
from qa_job_bot.document_policy import (
    bind_visual_review,
    content_hash,
    validate_draft,
    validate_review,
)
from qa_job_bot.openai_transport import OpenAITransport


def rendered_page(color="white", digest="b" * 64):
    image = Image.new("RGB", (120, 160), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    return {
        "sha256": digest,
        "page_count": 1,
        "pages": [
            {
                "page": 1,
                "width": 120,
                "height": 160,
                "image_sha256": hashlib.sha256(content).hexdigest(),
                "image_base64": base64.b64encode(content).decode(),
            }
        ],
    }


@pytest.mark.parametrize("source_format", ["docx", "pdf"])
async def test_agent_mcp_and_openai_http_contract_remain_isolated(monkeypatch, source_format):
    draft = {
        "title": "Curriculum Vitae",
        "language": "en",
        "sections": [
            {
                "heading": "Experience",
                "paragraphs": [{"text": "QA Engineer at Acme", "fact_ids": ["f1"], "kind": "role"}],
            }
        ],
    }
    context = {
        "owner_id": "synthetic-owner",
        "task_id": "synthetic-task",
        "kind": "cv",
        "language": "en",
        "job": {"title": "QA Engineer", "description": "Test web applications and APIs."},
        "template": {
            "id": "synthetic-template",
            "source_sha256": "a" * 64,
            "facts": [{"id": "f1", "text": "QA Engineer at Acme", "confirmed": True}],
            "style": {"source_format": source_format},
        },
    }
    if source_format == "pdf":
        context["template"]["reference"] = rendered_page("gray", "a" * 64)
    rendered = rendered_page()
    api_key = "sk-" + "x" * 24
    sessions = {}
    calls = []

    def provider(request):
        calls.append(request)
        assert request.url.host == "api.openai.com"
        assert request.headers["authorization"] == f"Bearer {api_key}"
        assert request.headers["openai-beta"] == "agents=v1"
        path = request.url.path
        if path == "/v1/agents/sessions":
            body = json.loads(request.content)
            assert body["environment"] == {"type": "none"}
            assert body["vault_ids"] == []
            assert body["agent"]["tools"] == []
            assert body["agent"]["multi_agent"] == {"enabled": False}
            role = (
                "visual"
                if "visual document reviewer" in body["agent"]["instructions"]
                else (
                    "reviewer"
                    if "factual reviewer" in body["agent"]["instructions"]
                    else "generator"
                )
            )
            session_id = f"sess_{role}"
            content = body["input"][0]["content"]
            if role == "visual":
                expected = ["input_text", "input_text", "input_image"]
                if source_format == "pdf":
                    expected += ["input_text", "input_image"]
                assert [part["type"] for part in content] == expected
                references = [
                    reference["reference"]
                    for part in content
                    if part["type"] == "input_text"
                    for reference in json.loads(part["text"]).get("page_references", [])
                ]
                assert references == (
                    ["original_template", "rendered_document"]
                    if source_format == "pdf"
                    else ["rendered_document"]
                )
                assert "a" * 64 in content[0]["text"]
                assert "image_base64" not in content[0]["text"]
                schema = body["agent"]["text"]["format"]["schema"]
                response = {
                    "verdict": "pass",
                    "issues": [],
                    **{
                        name: schema["properties"][name]["enum"][0]
                        for name in ("template_sha256", "rendered_sha256", "draft_hash")
                    },
                }
            elif role == "reviewer":
                assert "get_document_draft" in content[0]["text"]
                response = {
                    "verdict": "pass",
                    "issues": [],
                    "draft_hash": content_hash(draft),
                    "checked_fact_ids": ["f1"],
                    "assessment": {
                        name: {"passed": True, "issues": []}
                        for name in (
                            "vacancy_alignment",
                            "evidence_selection",
                            "document_structure",
                        )
                    },
                }
            else:
                assert "get_document_draft" not in content[0]["text"]
                response = draft
            sessions[session_id] = {
                "id": session_id,
                "agent": body["agent"] | {"id": body["agent_id"]},
                "environment": body["environment"],
                "vault_ids": body["vault_ids"],
                "status": "in_progress",
                "required_actions": [],
                "response": response,
            }
            public = {
                key: value for key, value in sessions[session_id].items() if key != "response"
            }
            return httpx.Response(201, json=public)
        session_id = path.split("/")[4]
        session = sessions[session_id]
        if path.endswith("/turns"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": f"turn_{session_id.removeprefix('sess_')}",
                            "session_id": session_id,
                            "subagent_id": None,
                            "status": "completed",
                        }
                    ],
                    "has_more": False,
                },
            )
        if path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "turn_id": f"turn_{session_id.removeprefix('sess_')}",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": json.dumps(session["response"])}
                            ],
                        }
                    ],
                    "has_more": False,
                },
            )
        raise AssertionError(path)

    transport = httpx.MockTransport(provider)
    monkeypatch.setattr(
        agent_gateway,
        "OpenAITransport",
        lambda **kwargs: OpenAITransport(**kwargs, http_transport=transport),
    )
    settings = agent_gateway.OpenAISettings(
        api_key, "agent_synthetic", "gpt-5.6-luna", "medium"
    )
    gateway = agent_gateway.AgentGateway(settings=settings)
    generated = validate_draft(await gateway.generate(context), context)
    reviewed = validate_review(await gateway.review(context, generated), generated, context)
    visual = bind_visual_review(
        await gateway.visual_review(context, generated, rendered),
        generated,
        {"sha256": "a" * 64},
        rendered,
    )
    assert reviewed["verdict"] == visual["verdict"] == "pass"
    assert [request.url.path for request in calls if request.method == "POST"] == [
        "/v1/agents/sessions"
    ] * 3
    assert set(sessions) == {"sess_generator", "sess_reviewer", "sess_visual"}
