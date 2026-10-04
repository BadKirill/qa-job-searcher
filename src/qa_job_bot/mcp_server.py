import argparse
import copy
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

MAX_CONTEXT_BYTES = 512_000
TOOLS = ("get_job_context", "get_candidate_facts", "get_template_metadata")


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
