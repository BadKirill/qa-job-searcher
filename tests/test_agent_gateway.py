import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai_codex import ApprovalMode, ExternalMessage, Sandbox
from openai_codex.generated.v2_all import TurnStatus

from qa_job_bot.agent_gateway import (
    DISABLED_FEATURES,
    AgentGateway,
    AgentUnavailable,
    JsonResponse,
    _deny_request,
    safe_overrides,
    verify_config,
)
from qa_job_bot.mcp_server import TOOLS, TaskScope, scope_context


def safe_config():
    return {
        "features": dict.fromkeys(DISABLED_FEATURES, False),
        "web_search": "disabled",
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "notify": [],
        "mcp_servers": {"qa_task": {"enabled": True}, "desktop": {"enabled": False}},
        "plugins": {"desktop": {"enabled": False}},
    }


def context():
    return {
        "task_id": "test",
        "kind": "cv",
        "language": "en",
        "job": {"title": "QA"},
        "template": {"id": "v1", "facts": [{"id": "f1", "text": "QA Engineer"}]},
    }


def draft():
    return {
        "title": "CV",
        "language": "en",
        "sections": [
            {"heading": "Experience", "paragraphs": [{"text": "QA Engineer", "fact_ids": ["f1"]}]}
        ],
    }


def test_effective_config_and_overrides():
    verify_config(safe_config(), "qa_task")
    config = safe_config()
    config["mcp_servers"]["qa_task"]["enabled"] = False
    verify_config(config)
    overrides = safe_overrides(config)
    assert 'mcp_servers={"qa_task"={enabled=false},"desktop"={enabled=false}}' in overrides
    assert 'plugins={"desktop"={enabled=false}}' in overrides
    assert "features.shell_tool=false" in safe_overrides()


@pytest.mark.parametrize(
    "change",
    [
        {"features": {}},
        {"web_search": "live"},
        {"approval_policy": "on-request"},
        {"sandbox_mode": "workspace-write"},
        {"notify": ["shell"]},
        {"mcp_servers": {"attacker": {}}},
        {"plugins": {"attacker": {}}},
    ],
)
def test_rejects_unsafe_effective_configuration(change):
    with pytest.raises(AgentUnavailable):
        verify_config(safe_config() | change, "qa_task")


@pytest.mark.parametrize(
    "method,expected",
    [
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
        ("unknown", {"error": "Operation is not authorized"}),
    ],
)
def test_approval_handler_denies(method, expected):
    assert _deny_request(method, {"command": "dangerous"}) == expected


def test_client_launch_drops_inherited_environment(tmp_path):
    gateway = AgentGateway(codex_bin=sys.executable)
    client = gateway._client(tmp_path, safe_overrides())
    command = client._client._sync.config.launch_args_override
    assert command[:2] == ("/usr/bin/env", "-i")
    assert "OPENAI_API_KEY" not in " ".join(command)
    assert client._client._sync._approval_handler is _deny_request
    assert client._client._sync.config.cwd == str(tmp_path)


class FakeClient:
    def __init__(self, config, tools=None, account_type="chatgpt", result=None):
        self.config = config
        self.inventory = (
            tools if tools is not None else [{"name": "qa_task", "tools": dict.fromkeys(TOOLS, {})}]
        )
        self.account_type = account_type
        self.request = AsyncMock(side_effect=self.respond)
        self._client = SimpleNamespace(request=self.request)
        self.thread = SimpleNamespace(
            run=AsyncMock(
                return_value=result
                or SimpleNamespace(
                    status=TurnStatus.completed,
                    final_response=json.dumps(draft()),
                )
            )
        )
        self.thread_start = AsyncMock(return_value=self.thread)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def account(self):
        data = (
            None
            if self.account_type is None
            else SimpleNamespace(model_dump=lambda: {"type": self.account_type})
        )
        return SimpleNamespace(account=data)

    async def respond(self, method, *args, **kwargs):
        return JsonResponse(
            {"config": self.config} if method == "config/read" else {"data": self.inventory}
        )


def setup_clients(monkeypatch, **kwargs):
    probe = FakeClient(safe_config())
    runner = FakeClient(safe_config(), **kwargs)
    gateway = AgentGateway(codex_bin=sys.executable)
    clients = iter([probe, runner])
    monkeypatch.setattr(gateway, "_client", lambda *args: next(clients))
    monkeypatch.setattr(
        gateway, "_mcp_snapshot", AsyncMock(return_value={"get_candidate_facts": {"facts": []}})
    )
    return gateway, runner


async def test_generate_isolated_context_and_no_personal_facts_in_instructions(monkeypatch):
    gateway, runner = setup_clients(monkeypatch)
    assert (
        await gateway.generate(
            context()
            | {
                "revision_feedback": ["Check role"],
                "owner_messages": ["Keep concise"],
            }
        )
        == draft()
    )
    options = runner.thread_start.call_args.kwargs
    assert options["ephemeral"] is True
    assert options["sandbox"] == Sandbox.read_only
    assert options["approval_mode"] == ApprovalMode.deny_all
    request = runner.thread.run.call_args.args[0]
    assert isinstance(request, ExternalMessage)
    assert json.loads(request.content)["revision_feedback"] == ["Check role"]
    assert json.loads(request.content)["owner_messages"] == ["Keep concise"]
    assert "completed" in gateway.last_diagnostic


async def test_review_separate_role_and_draft_hash(monkeypatch):
    gateway, runner = setup_clients(
        monkeypatch,
        tools=[{"name": "qa_task", "tools": dict.fromkeys([*TOOLS, "get_document_draft"], {})}],
    )
    await gateway.review(context(), draft())
    request = json.loads(runner.thread.run.call_args.args[0].content)
    assert len(request["draft_hash"]) == 64
    assert (
        "independent factual reviewer"
        in runner.thread_start.call_args.kwargs["developer_instructions"]
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_type": None},
        {"account_type": "apiKey"},
        {"tools": []},
        {"tools": [{"name": "evil", "tools": {"run_shell": {}}}]},
        {"tools": [{"name": "qa_task", "tools": {"run_shell": {}}}]},
        {"result": SimpleNamespace(status=TurnStatus.failed, final_response=None)},
        {"result": SimpleNamespace(status=TurnStatus.completed, final_response="invalid")},
    ],
)
async def test_runtime_failures_fail_closed(monkeypatch, kwargs):
    gateway, _ = setup_clients(monkeypatch, **kwargs)
    with pytest.raises(AgentUnavailable):
        await gateway.generate(context())


async def test_runtime_unavailable_timeout_and_failure(monkeypatch):
    with pytest.raises(AgentUnavailable, match="not installed"):
        await AgentGateway(codex_bin="/nonexistent").generate(context())
    gateway = AgentGateway(codex_bin=sys.executable)
    monkeypatch.setattr(gateway, "_isolated_run", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(AgentUnavailable, match="TimeoutError"):
        await gateway.generate(context())


async def test_chat_and_empty_responses(monkeypatch):
    gateway = AgentGateway()
    mocked = AsyncMock(return_value={"reply": "Please confirm the facts in the form."})
    monkeypatch.setattr(gateway, "_run", mocked)
    assert "confirm" in await gateway.chat(context(), [{"role": "user", "content": "Help"}])
    assert mocked.call_args.args[1] == "chat"
    for response in ({"reply": " "}, {"reply": 3}, {}):
        mocked.return_value = response
        with pytest.raises(AgentUnavailable):
            await gateway.chat(context(), [{"role": "user", "content": "Help"}])
    for messages in ([], [{"content": "x" * 60001}]):
        with pytest.raises(ValueError):
            await gateway.chat(context(), messages)


async def test_real_mcp_prefetch_roundtrip(tmp_path):
    scoped = scope_context(context())
    scope = TaskScope("owner", "test", "v1", time.time() + 60, "generator")
    payload = tmp_path / "context.json"
    payload.write_text(json.dumps({"context": scoped, "scope": vars(scope)}))
    payload.chmod(0o600)
    snapshot = await AgentGateway()._mcp_snapshot(payload, list(TOOLS))
    assert snapshot["get_candidate_facts"]["facts"][0]["id"] == "f1"
    with pytest.raises(ExceptionGroup) as error:
        await AgentGateway()._mcp_snapshot(payload, ["run_shell"])
    assert "MCP tools" in repr(error.value)
