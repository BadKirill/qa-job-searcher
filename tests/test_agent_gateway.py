import base64
import hashlib
import io
import json
import sys
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai_codex import ApprovalMode, ExternalMessage, Sandbox
from openai_codex._inputs import _to_wire_turn_input
from openai_codex.generated.v2_all import TurnStatus
from PIL import Image

from qa_job_bot.agent_gateway import (
    DISABLED_FEATURES,
    AgentGateway,
    AgentUnavailable,
    JsonResponse,
    _deny_request,
    reference_images,
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


def visual_reference(size=(320, 400), color="white"):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    return {
        "sha256": "a" * 64,
        "page_count": 1,
        "pages": [
            {
                "page": 1,
                "width": size[0],
                "height": size[1],
                "image_base64": base64.b64encode(raw).decode(),
                "image_sha256": hashlib.sha256(raw).hexdigest(),
            }
        ],
    }


def visual_context():
    data = context()
    data["template"]["reference"] = visual_reference()
    return data


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


def test_visual_reference_png_validation_and_manifest():
    reference = visual_reference()
    manifest, images = reference_images(reference)
    assert manifest["sha256"] == reference["sha256"]
    assert "image_base64" not in manifest["pages"][0]
    assert len(images) == 1
    assert images[0]["type"] == "input_image"
    assert images[0]["detail"] == "high"
    assert (
        images[0]["image_url"] == "data:image/png;base64," + reference["pages"][0]["image_base64"]
    )
    assert reference_images(None) == (None, [])


@pytest.mark.parametrize(
    "change",
    [
        {"image_base64": None},
        {"image_base64": "https://external.example/image.png"},
        {"image_base64": "file:///private/cv.png"},
        {"image_base64": "%%%"},
        {"image_base64": base64.b64encode(b"not PNG").decode()},
        {"image_sha256": "b" * 64},
        {"width": 319},
        {"height": 399},
    ],
)
def test_visual_reference_rejects_unsafe_or_mismatched_png(change):
    reference = visual_reference()
    reference["pages"][0].update(change)
    with pytest.raises(ValueError):
        reference_images(reference)


def test_visual_reference_encoded_and_total_bytes_limits(monkeypatch):
    reference = visual_reference()
    encoded = reference["pages"][0]["image_base64"]
    raw_length = len(base64.b64decode(encoded))
    monkeypatch.setattr("qa_job_bot.agent_gateway.MAX_REFERENCE_ENCODED", len(encoded))
    monkeypatch.setattr("qa_job_bot.agent_gateway.MAX_REFERENCE_BYTES", raw_length)
    assert len(reference_images(reference)[1]) == 1
    two_pages = deepcopy(reference)
    two_pages["page_count"] = 2
    second = deepcopy(two_pages["pages"][0])
    second["page"] = 2
    two_pages["pages"].append(second)
    with pytest.raises(ValueError, match="encoded size"):
        reference_images(two_pages)
    monkeypatch.setattr("qa_job_bot.agent_gateway.MAX_REFERENCE_ENCODED", len(encoded) * 2)
    with pytest.raises(ValueError, match="PNG size"):
        reference_images(two_pages)


def test_visual_reference_rejects_corrupted_crc_and_animation():
    reference = visual_reference()
    corrupted = bytearray(base64.b64decode(reference["pages"][0]["image_base64"]))
    corrupted[corrupted.index(b"IDAT") + 5] ^= 1
    reference["pages"][0].update(
        image_base64=base64.b64encode(corrupted).decode(),
        image_sha256=hashlib.sha256(corrupted).hexdigest(),
    )
    with pytest.raises((SyntaxError, OSError)):
        reference_images(reference)
    output = io.BytesIO()
    Image.new("RGB", (320, 400), "red").save(
        output,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (320, 400), "blue")],
        duration=100,
        loop=0,
    )
    animated = output.getvalue()
    reference["pages"][0].update(
        image_base64=base64.b64encode(animated).decode(),
        image_sha256=hashlib.sha256(animated).hexdigest(),
    )
    with pytest.raises(ValueError, match="Animated"):
        reference_images(reference)


async def test_multimodal_external_message_uses_sdk_wire_schema_and_no_image_tools(monkeypatch):
    gateway, runner = setup_clients(monkeypatch)
    await gateway.generate(visual_context())
    message = runner.thread.run.call_args.args[0]
    assert isinstance(message, ExternalMessage)
    assert isinstance(message.content, list)
    assert [item["type"] for item in message.content] == ["input_text", "input_text", "input_image"]
    text = json.loads(message.content[0]["text"])
    assert text["visual_references"]["original_template"]["page_count"] == 1
    assert "image_base64" not in json.dumps(text)
    inputs, tool_output = _to_wire_turn_input(message)
    assert inputs == []
    serialized = tool_output.model_dump(mode="json")
    assert serialized["output"][2]["image_url"].startswith("data:image/png;base64,")
    assert serialized["output"][2]["detail"] == "high"
    options = runner.thread_start.call_args.kwargs
    assert "company/date entries" in options["developer_instructions"]
    assert "images show document appearance" in options["base_instructions"]
    assert "view_image" in DISABLED_FEATURES
    assert "image_base64" not in json.dumps(gateway.last_run_metadata)
    paragraph = runner.thread.run.call_args.kwargs["output_schema"]["$defs"]["Paragraph"]
    assert set(paragraph["required"]) == set(paragraph["properties"])
    assert "default" not in paragraph["properties"]["kind"]
    assert "do not reproduce CV sections" in options["developer_instructions"]


async def test_reviewer_receives_same_original_images_in_separate_turn(monkeypatch):
    gateway, runner = setup_clients(
        monkeypatch,
        tools=[{"name": "qa_task", "tools": dict.fromkeys([*TOOLS, "get_document_draft"], {})}],
    )
    await gateway.review(visual_context(), draft())
    message = runner.thread.run.call_args.args[0]
    assert (
        message.content[-1]["image_url"]
        == "data:image/png;base64,"
        + (visual_context()["template"]["reference"]["pages"][0]["image_base64"])
    )
    assert (
        "original template pages" in runner.thread_start.call_args.kwargs["developer_instructions"]
    )
    assert (
        "never require CV sections"
        in runner.thread_start.call_args.kwargs["developer_instructions"]
    )


async def test_visual_review_binds_both_references_and_draft_hash(monkeypatch):
    gateway, runner = setup_clients(
        monkeypatch,
        tools=[{"name": "qa_task", "tools": dict.fromkeys([*TOOLS, "get_document_draft"], {})}],
    )
    rendered = visual_reference(color="blue")
    rendered["sha256"] = "c" * 64
    await gateway.visual_review(visual_context(), draft(), rendered)
    message = runner.thread.run.call_args.args[0]
    data = json.loads(message.content[0]["text"])
    assert data["template_sha256"] == "a" * 64
    assert data["rendered_sha256"] == "c" * 64
    assert len(data["draft_hash"]) == 64
    assert len([item for item in message.content if item["type"] == "input_image"]) == 2
    assert "rendered_reference" not in data
    assert "base64" not in json.dumps(data)
    first = json.loads(message.content[1]["text"])["page_references"]
    second = json.loads(message.content[3]["text"])["page_references"]
    assert first[0]["reference"] == "original_template"
    assert second[0]["reference"] == "rendered_document"
    assert (
        "independent visual document reviewer"
        in runner.thread_start.call_args.kwargs["developer_instructions"]
    )


async def test_visual_review_requires_both_original_and_rendered_images():
    gateway = AgentGateway()
    with pytest.raises(ValueError, match="requires original"):
        await gateway.visual_review(context(), draft(), visual_reference())
    with pytest.raises(ValueError, match="requires original"):
        await gateway.visual_review(visual_context(), draft(), None)


async def test_visual_review_identical_pngs_keep_both_document_page_references(monkeypatch):
    gateway, runner = setup_clients(
        monkeypatch,
        tools=[{"name": "qa_task", "tools": dict.fromkeys([*TOOLS, "get_document_draft"], {})}],
    )
    rendered = visual_reference()
    rendered["sha256"] = "c" * 64
    await gateway.visual_review(visual_context(), draft(), rendered)
    message = runner.thread.run.call_args.args[0]
    assert [item["type"] for item in message.content] == ["input_text", "input_text", "input_image"]
    references = json.loads(message.content[1]["text"])["page_references"]
    assert [item["reference"] for item in references] == ["original_template", "rendered_document"]
    assert [item["source_sha256"] for item in references] == ["a" * 64, "c" * 64]
    assert references[0]["image_sha256"] == references[1]["image_sha256"]
    assert "byte-identical PNGs" in runner.thread_start.call_args.kwargs["base_instructions"]


async def test_invalid_image_blocks_provider_start_and_is_not_logged(monkeypatch):
    gateway = AgentGateway(codex_bin=sys.executable)
    start = AsyncMock()
    monkeypatch.setattr(gateway, "_client", start)
    data = visual_context()
    data["template"]["reference"]["pages"][0]["image_base64"] = "private-content-invalid"
    with pytest.raises(AgentUnavailable):
        await gateway.generate(data)
    start.assert_not_called()
    assert "private" not in gateway.last_diagnostic


async def test_invalid_rendered_image_blocks_visual_reviewer_before_provider_start(monkeypatch):
    gateway = AgentGateway(codex_bin=sys.executable)
    start = AsyncMock()
    monkeypatch.setattr(gateway, "_client", start)
    rendered = visual_reference()
    rendered["pages"][0]["image_sha256"] = "f" * 64
    with pytest.raises(AgentUnavailable):
        await gateway.visual_review(visual_context(), draft(), rendered)
    start.assert_not_called()
