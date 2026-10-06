import argparse
import copy
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

MAX_CONTEXT_BYTES = 512_000
MAX_REFERENCE_PAGES = 20
MAX_REFERENCE_SIDE = 1400
MAX_REFERENCE_PIXELS = 30_000_000
TOOLS = ("get_job_context", "get_candidate_facts", "get_template_metadata")


def reference_manifest(reference: dict | None) -> dict | None:
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"sha256", "page_count", "pages"}:
        raise ValueError("Invalid visual reference structure")
    source_hash = reference["sha256"]
    count, pages = reference["page_count"], reference["pages"]
    if not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("Invalid visual reference hash")
    if type(count) is not int or not 1 <= count <= MAX_REFERENCE_PAGES:
        raise ValueError("Visual reference page limit exceeded")
    if not isinstance(pages, list) or len(pages) != count:
        raise ValueError("Visual reference page count mismatch")
    result, pixels = [], 0
    for index, page in enumerate(pages, 1):
        metadata = {"page", "width", "height", "image_sha256"}
        if not isinstance(page, dict) or not metadata <= set(page) <= metadata | {"image_base64"}:
            raise ValueError("Invalid visual reference page structure")
        if type(page["page"]) is not int or page["page"] != index:
            raise ValueError("Visual reference pages must be consecutive")
        dimensions = (page["width"], page["height"])
        if any(
            type(value) is not int or not 1 <= value <= MAX_REFERENCE_SIDE for value in dimensions
        ):
            raise ValueError("Visual reference dimensions exceeded")
        image_hash = page["image_sha256"]
        if not isinstance(image_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", image_hash):
            raise ValueError("Invalid visual reference page hash")
        pixels += page["width"] * page["height"]
        result.append({key: page[key] for key in sorted(metadata)})
    if pixels > MAX_REFERENCE_PIXELS:
        raise ValueError("Visual reference pixel limit exceeded")
    return {"sha256": source_hash, "page_count": count, "pages": result}


@dataclass(frozen=True)
class TaskScope:
    owner_id: str
    task_id: str
    template_version: str
    expires_at: float
    role: str

    def check(self, context: dict) -> None:
        expected = (
            self.owner_id,
            self.task_id,
            self.template_version,
        )
        actual = (
            str(context.get("owner_id", "")),
            str(context.get("task_id", "")),
            str(context.get("template", {}).get("id", "")),
        )
        if not all(expected) or actual != expected or time.time() >= self.expires_at:
            raise PermissionError("Task permission is missing, expired or mismatched")
        if self.role not in {"generator", "reviewer", "chat"}:
            raise PermissionError("Unknown role")


def scope_context(context: dict) -> dict:
    template = context.get("template") or {}
    result = {
        "owner_id": str(context.get("owner_id", "owner")),
        "task_id": str(context["task_id"]),
        "kind": context.get("kind", "chat"),
        "language": context.get("language", "en"),
        "job": copy.deepcopy(context.get("job") or {}),
        "template": {
            key: copy.deepcopy(template.get(key, default))
            for key, default in (
                ("id", "draft"),
                ("facts", []),
                ("style", {}),
                ("instructions", ""),
            )
        },
    }
    reference = reference_manifest(template.get("reference"))
    if reference is not None:
        result["template"]["reference"] = reference
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("Task context is too large")
    return result


class ScopedServer(FastMCP):
    def __init__(self, scope: TaskScope, context: dict):
        self.scope = scope
        self.context = context
        super().__init__("QA document task", log_level="ERROR")

    async def call_tool(self, name: str, arguments: dict):
        self.scope.check(self.context)
        if arguments:
            raise PermissionError("Task tools do not accept identifiers, paths or other arguments")
        return await super().call_tool(name, arguments)

    async def list_tools(self):
        self.scope.check(self.context)
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema["additionalProperties"] = False
        return tools


def create_server(context: dict, scope: TaskScope, draft: dict | None = None) -> FastMCP:
    data = copy.deepcopy(context)
    scoped_draft = copy.deepcopy(draft)
    scope.check(data)
    server = ScopedServer(scope, data)
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=annotations)
    def get_job_context() -> dict:
        scope.check(data)
        return copy.deepcopy(data["job"])

    @server.tool(annotations=annotations)
    def get_candidate_facts() -> dict:
        scope.check(data)
        return {
            "version": scope.template_version,
            "facts": copy.deepcopy(data["template"]["facts"]),
        }

    @server.tool(annotations=annotations)
    def get_template_metadata() -> dict:
        scope.check(data)
        return {
            "version": scope.template_version,
            "style": copy.deepcopy(data["template"]["style"]),
            "instructions": data["template"]["instructions"],
            "reference": reference_manifest(data["template"].get("reference")),
        }

    if scope.role == "reviewer":

        @server.tool(annotations=annotations)
        def get_document_draft() -> dict:
            scope.check(data)
            if scoped_draft is None:
                raise PermissionError("No draft assigned to this reviewer")
            return copy.deepcopy(scoped_draft)

    return server


def load_payload(path: Path) -> dict:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise PermissionError("Task context must be a private regular file")
    if metadata.st_uid != os.getuid() or metadata.st_size > MAX_CONTEXT_BYTES:
        raise PermissionError("Invalid task context owner or size")
    return json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True, type=Path)
    args = parser.parse_args()
    payload = load_payload(args.context)
    scope = TaskScope(**payload["scope"])
    create_server(payload["context"], scope, payload.get("draft")).run(transport="stdio")


if __name__ == "__main__":
    main()
