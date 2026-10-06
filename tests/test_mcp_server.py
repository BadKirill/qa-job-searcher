import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from qa_job_bot.mcp_server import (
    MAX_CONTEXT_BYTES,
    TOOLS,
    TaskScope,
    create_server,
    load_payload,
    main,
    reference_manifest,
    scope_context,
)


def context():
    return scope_context(
        {
            "task_id": "job-1",
            "owner_id": "owner",
            "job": {"title": "QA"},
            "template": {
                "id": "v1",
                "facts": [{"id": "f1", "text": "QA"}],
                "path": "/secret/cv.docx",
            },
        }
    )


def scope(role="generator", expires=None):
    return TaskScope("owner", "job-1", "v1", expires or time.time() + 60, role)


def visual_reference():
    return {
        "sha256": "a" * 64,
        "page_count": 1,
        "pages": [
            {
                "page": 1,
                "width": 100,
                "height": 200,
                "image_sha256": "b" * 64,
                "image_base64": "image-data-never-in-MCP",
            }
        ],
    }


@pytest.mark.parametrize("role", ["generator", "reviewer", "chat"])
async def test_in_process_server_scopes_and_copy(role):
    source = context()
    server = create_server(source, scope(role), {"title": "CV"})
    source["job"]["title"] = "Changed"
    tools = await server.list_tools()
    names = set(TOOLS) | ({"get_document_draft"} if role == "reviewer" else set())
    assert {tool.name for tool in tools} == names
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert "QA" in str(await server.call_tool("get_job_context", {}))
    assert "/secret" not in str(await server.call_tool("get_template_metadata", {}))
    assert "f1" in str(await server.call_tool("get_candidate_facts", {}))
    if role == "reviewer":
        assert "CV" in str(await server.call_tool("get_document_draft", {}))
    with pytest.raises(Exception, match="Unknown tool"):
        await server.call_tool("run_shell", {})
    with pytest.raises(PermissionError):
        await server.call_tool("get_job_context", {"task_id": "job-2"})


@pytest.mark.parametrize(
    "field,value", [("owner_id", "other"), ("task_id", "job-2"), ("owner_id", "")]
)
def test_context_identity_mismatch(field, value):
    source = context()
    source[field] = value
    with pytest.raises(PermissionError):
        scope().check(source)


def test_scope_expiry_role_and_template():
    with pytest.raises(PermissionError):
        scope(expires=time.time() - 1).check(context())
    with pytest.raises(PermissionError):
        scope("admin").check(context())
    source = context()
    source["template"]["id"] = "v2"
    with pytest.raises(PermissionError):
        scope().check(source)
    with pytest.raises(ValueError):
        scope_context({"task_id": "x", "job": {"data": "x" * MAX_CONTEXT_BYTES}})
    assert scope_context({"task_id": "x"})["template"]["id"] == "draft"


async def test_reviewer_missing_draft_and_expired_calls(monkeypatch):
    server = create_server(context(), scope("reviewer"))
    with pytest.raises(Exception, match="No draft"):
        await server.call_tool("get_document_draft", {})
    monkeypatch.setattr("qa_job_bot.mcp_server.time.time", lambda: 9999999999.0)
    with pytest.raises(PermissionError):
        await server.list_tools()
    with pytest.raises(PermissionError):
        await server.call_tool("get_job_context", {})


def test_private_payload_validation(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    path.write_text('{"value": 1}')
    path.chmod(0o600)
    assert load_payload(path) == {"value": 1}
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_payload(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(PermissionError):
        load_payload(link)
    with pytest.raises(PermissionError):
        load_payload(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: -1)
    with pytest.raises(PermissionError):
        load_payload(path)


def test_large_payload_and_cli(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    path.write_text("x" * (MAX_CONTEXT_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(PermissionError):
        load_payload(path)
    path.write_text(json.dumps({"context": context(), "scope": vars(scope())}))
    called = []
    monkeypatch.setattr(sys, "argv", ["mcp", "--context", str(path)])
    monkeypatch.setattr(
        "qa_job_bot.mcp_server.ScopedServer.run", lambda _, transport: called.append(transport)
    )
    main()
    assert called == ["stdio"]


async def test_actual_stdio_transport_rejects_cross_task_access(tmp_path):
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"context": context(), "scope": vars(scope())}))
    path.chmod(0o600)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qa_job_bot.mcp_server", "--context", str(path)],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as client:
        assert (await client.initialize()).serverInfo.name == "QA document task"
        assert {tool.name for tool in (await client.list_tools()).tools} == set(TOOLS)
        response = await client.call_tool("get_job_context", {})
        assert not response.isError
        assert "QA" in response.content[0].text
        assert (await client.call_tool("get_job_context", {"task_id": "other"})).isError
        assert (await client.call_tool("get_document_draft", {})).isError
        assert (await client.call_tool("run_shell", {"command": "id"})).isError


async def test_mcp_template_manifest_omits_image_and_raw_text_payload():
    source = context()
    source["template"].update(reference=visual_reference(), raw_blocks=[{"text": "unconfirmed"}])
    scoped = scope_context(source)
    assert "image_base64" not in json.dumps(scoped)
    assert "raw_blocks" not in scoped["template"]
    server = create_server(source, scope())
    result = str(await server.call_tool("get_template_metadata", {}))
    assert "page_count" in result
    assert "image-data" not in result
    assert "unconfirmed" not in result
    assert scoped["template"]["facts"] == source["template"]["facts"]


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "invalid"},
        {"sha256": 123},
        {"page_count": 0},
        {"page_count": 21},
        {"page_count": True},
        {"pages": []},
        {"pages": "bad"},
        {"path": "/private/template.pdf"},
    ],
)
def test_visual_manifest_rejects_invalid_reference(change):
    with pytest.raises(ValueError):
        reference_manifest(visual_reference() | change)
    assert reference_manifest(None) is None


@pytest.mark.parametrize(
    "change",
    [
        {"page": 2},
        {"page": True},
        {"width": 0},
        {"width": 1401},
        {"height": True},
        {"image_sha256": "invalid"},
        {"image_sha256": None},
        {"url": "https://external.example"},
    ],
)
def test_visual_manifest_rejects_invalid_page_metadata(change):
    reference = visual_reference()
    reference["pages"][0].update(change)
    with pytest.raises(ValueError):
        reference_manifest(reference)


def test_visual_manifest_page_structure_pixel_limit_and_deep_copy():
    reference = visual_reference()
    for bad in (None, "image", {"page": 1}):
        reference["pages"] = [bad]
        with pytest.raises(ValueError):
            reference_manifest(reference)
    with pytest.raises(ValueError):
        reference_manifest("reference")
    reference = visual_reference()
    reference["pages"][0].update(width=1400, height=1400)
    reference["pages"] = [
        deepcopy(reference["pages"][0]) | {"page": index} for index in range(1, 16)
    ]
    reference["page_count"] = 15
    manifest = reference_manifest(reference)
    assert manifest["page_count"] == 15
    reference["pages"][0]["width"] = 100
    assert manifest["pages"][0]["width"] == 1400
    reference["pages"][0]["width"] = 1400
    reference["page_count"] = 16
    reference["pages"].append(deepcopy(reference["pages"][0]) | {"page": 16})
    with pytest.raises(ValueError, match="pixel limit"):
        reference_manifest(reference)


def test_visual_manifest_accepts_page_count_and_pixel_boundaries():
    reference = visual_reference()
    reference["pages"] = [deepcopy(reference["pages"][0]) | {"page": i} for i in range(1, 21)]
    reference["page_count"] = 20
    assert reference_manifest(reference)["page_count"] == 20
    reference["pages"] = [
        {"page": i, "width": 1400, "height": 1400, "image_sha256": "b" * 64} for i in range(1, 16)
    ]
    reference["pages"].append({"page": 16, "width": 1000, "height": 600, "image_sha256": "b" * 64})
    reference["page_count"] = 16
    assert reference_manifest(reference)["page_count"] == 16
    reference["pages"][-1]["height"] += 1
    with pytest.raises(ValueError, match="pixel limit"):
        reference_manifest(reference)
