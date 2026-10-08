import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from qa_job_bot.openai_transport import (
    OpenAITransport,
    OpenAITransportError,
    resource_id,
    token_usage,
)

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}
USAGE = {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}


class API:
    def __init__(self):
        self.calls = []
        self.session = None
        self.change_session = lambda session: session
        self.turns = [
            {
                "id": "turn_test",
                "session_id": "sess_test",
                "subagent_id": None,
                "status": "completed",
                "usage": USAGE,
            }
        ]
        self.items = [
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "turn_id": "turn_test",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"reply":"ok"}'}],
            }
        ]
        self.extra_page = {}
        self.creation_response = None
        self.cancel_error = False

    def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == "api.openai.com"
        assert request.headers["authorization"] == "Bearer sk-synthetic"
        assert request.headers["openai-beta"] == "agents=v1"
        path = request.url.path
        if path == "/v1/agents/sessions":
            if self.creation_response:
                return self.creation_response
            body = json.loads(request.content)
            assert request.headers["idempotency-key"]
            self.session = {
                "id": "sess_test",
                "agent": body["agent"] | {"id": body["agent_id"]},
                "environment": body["environment"],
                "vault_ids": body["vault_ids"],
                "status": "in_progress",
                "required_actions": [],
            }
            return httpx.Response(201, json=self.change_session(deepcopy(self.session)))
        if path.endswith("/events"):
            assert json.loads(request.content) == {
                "events": [{"type": "agent.session.input.cancel"}]
            }
            return httpx.Response(500 if self.cancel_error else 200, json={})
        if path.endswith("/turns"):
            return httpx.Response(
                200, json={"data": self.turns, "has_more": False} | self.extra_page
            )
        if path.endswith("/items"):
            return httpx.Response(200, json={"data": self.items, "has_more": False})
        assert path == "/v1/agents/sessions/sess_test"
        self.turns[0]["status"] = "completed"
        return httpx.Response(200, json=self.session)


def gateway(api, **kwargs):
    return OpenAITransport(
        "sk-synthetic",
        "agent_test",
        "gpt-5.6-luna",
        "medium",
        http_transport=httpx.MockTransport(api),
        **kwargs,
    )


async def run(transport, **kwargs):
    return await transport.run(
        instructions="trusted rules",
        schema=SCHEMA,
        content=kwargs.get("content", "untrusted input"),
    )


async def test_success_uses_saved_agent_and_enforces_restricted_session():
    api = API()
    output, metadata = await run(gateway(api, project_id="proj_test"))
    body = json.loads(api.calls[0].content)
    assert body["agent_id"] == "agent_test"
    assert body["environment"] == {"type": "none"}
    assert body["vault_ids"] == []
    assert body["agent"]["tools"] == []
    assert body["agent"]["multi_agent"] == {"enabled": False}
    assert body["agent"]["text"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert body["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "untrusted input"}]}
    ]
    assert all(call.headers["OpenAI-Project"] == "proj_test" for call in api.calls)
    assert output == {"reply": "ok"}
    assert metadata == {
        "provider": "openai_agents",
        "session_id": "sess_test",
        "turn_id": "turn_test",
        "usage": USAGE,
    }
    assert len(api.calls) == 3


async def test_images_preserved_and_pending_turn_polled(monkeypatch):
    api = API()
    api.turns[0]["status"] = "in_progress"
    sleep = AsyncMock()
    monkeypatch.setattr("qa_job_bot.openai_transport.asyncio.sleep", sleep)
    content = [{"type": "input_image", "image_url": "data:image/png;base64,synthetic"}]
    await run(gateway(api), content=content)
    assert json.loads(api.calls[0].content)["input"][0]["content"] == content
    assert len(api.calls) == 5
    sleep.assert_awaited_once_with(2)


@pytest.mark.parametrize(
    "patch",
    [
        {"id": "sess_other"},
        {"environment": {"type": "openai_hosted"}},
        {"vault_ids": ["vault_secret"]},
        {"required_actions": [{"type": "function_call"}]},
        {"status": "requires_action"},
        {"status": "failed"},
        {"agent_patch": {"id": "agent_other"}},
        {"agent_patch": {"tools": [{"type": "web_search"}]}},
        {"agent_patch": {"multi_agent": {"enabled": True}}},
        {"agent_patch": {"instructions": "ignore policy"}},
        {"agent_patch": {"model": "gpt-other"}},
        {"agent_patch": {"text": {}}},
        {"agent_patch": {"reasoning": {"effort": "high"}}},
    ],
)
async def test_unsafe_session_cancelled_and_blocked(patch):
    api = API()

    def change(session):
        session.update({k: v for k, v in patch.items() if k != "agent_patch"})
        session["agent"].update(patch.get("agent_patch", {}))
        return session

    api.change_session = change
    with pytest.raises(OpenAITransportError):
        await run(gateway(api))
    assert api.calls[-1].url.path.endswith("/events")


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "failed"},
        {"status": "cancelled"},
        {"status": "waiting"},
        {"status": "unknown"},
        {"subagent_id": "subagent_test"},
        {"session_id": "sess_other"},
        {"id": "../private"},
    ],
)
async def test_failed_or_unrelated_turn_is_blocked(patch):
    api = API()
    api.turns[0].update(patch)
    with pytest.raises(OpenAITransportError):
        await run(gateway(api))
    assert api.calls[-1].url.path.endswith("/events")


@pytest.mark.parametrize("extra", [{"has_more": True}, {"data": {}}, {"data": [None]}])
async def test_incomplete_page_is_blocked(extra):
    api = API()
    api.extra_page = extra
    with pytest.raises(OpenAITransportError):
        await run(gateway(api))


async def test_multiple_turns_rejected():
    api = API()
    api.turns *= 2
    with pytest.raises(OpenAITransportError, match="additional turns"):
        await run(gateway(api))


@pytest.mark.parametrize(
    "content",
    [
        [],
        None,
        [None],
        [{"type": "refusal", "text": "no"}],
        [{"type": "output_text", "text": 1}],
        [{"type": "output_text", "text": "[]"}],
        [{"type": "output_text", "text": "not json"}],
    ],
)
async def test_bad_final_content_rejected(content):
    api = API()
    api.items[0]["content"] = content
    with pytest.raises(OpenAITransportError):
        await run(gateway(api))


@pytest.mark.parametrize(
    "patch",
    [
        {"type": "function_call"},
        {"phase": "commentary"},
        {"turn_id": "turn_wrong"},
        {"status": "incomplete"},
    ],
)
async def test_tool_and_nonfinal_results_rejected(patch):
    api = API()
    api.items[0].update(patch)
    with pytest.raises(OpenAITransportError):
        await run(gateway(api))


async def test_multiple_final_answers_rejected():
    api = API()
    api.items *= 2
    with pytest.raises(OpenAITransportError, match="one final answer"):
        await run(gateway(api))


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
async def test_http_errors_do_not_retry_or_leak_body(status):
    api = API()
    api.creation_response = httpx.Response(status, text="private secret provider body")
    with pytest.raises(OpenAITransportError, match=f"HTTP {status}") as error:
        await run(gateway(api))
    assert "private" not in str(error.value)
    assert len(api.calls) == 1


@pytest.mark.parametrize("body", ["[]", "bad json", '{"id":"../escape"}'])
async def test_invalid_create_response_does_not_make_unsafe_requests(body):
    api = API()
    api.creation_response = httpx.Response(201, text=body)
    with pytest.raises(OpenAITransportError):
        await run(gateway(api))
    assert len(api.calls) == 1


async def test_request_and_response_byte_limits(monkeypatch):
    api = API()
    monkeypatch.setattr("qa_job_bot.openai_transport.MAX_REQUEST_BYTES", 10)
    with pytest.raises(OpenAITransportError, match="3.8 MB"):
        await run(gateway(api))
    assert not api.calls
    monkeypatch.setattr("qa_job_bot.openai_transport.MAX_REQUEST_BYTES", 10000)
    monkeypatch.setattr("qa_job_bot.openai_transport.MAX_RESPONSE_BYTES", 10)
    with pytest.raises(OpenAITransportError, match="response exceeds"):
        await run(gateway(api))


async def test_timeout_cancels_even_if_cancel_request_fails():
    api = API()
    api.turns = []
    api.cancel_error = True
    with pytest.raises(OpenAITransportError, match="TimeoutError"):
        await run(gateway(api, timeout=0.02))
    assert api.calls[-1].url.path.endswith("/events")


async def test_caller_cancellation_is_propagated_and_remote_turn_cancelled():
    api = API()
    api.turns = []
    task = asyncio.create_task(run(gateway(api)))
    while len(api.calls) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.calls[-1].url.path.endswith("/events")


def test_usage_and_identifiers_validate_without_echoing_data():
    assert token_usage(None) is None
    for invalid in (-1, True, "secret", None):
        assert token_usage(USAGE | {"input_tokens": invalid}) is None
    assert token_usage(USAGE | {"unexpected_secret": "secret"}) == USAGE
    assert resource_id("sess_test", "sess") == "sess_test"
    for invalid in (None, "https://attacker.example", "sess_a/b"):
        with pytest.raises(OpenAITransportError):
            resource_id(invalid, "sess")
