import asyncio
import json
import re
from contextlib import suppress
from uuid import uuid4

import httpx

MAX_REQUEST_BYTES = 3_800_000
MAX_RESPONSE_BYTES = 6_000_000
API_URL = "https://api.openai.com/v1"


class OpenAITransportError(RuntimeError):
    pass


def resource_id(value, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9]{1,128}", value):
        raise OpenAITransportError("OpenAI returned an invalid resource ID")
    return value


def token_usage(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    counts = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        count = value.get(name)
        if type(count) is not int or count < 0:
            return None
        counts[name] = count
    return counts


class OpenAITransport:
    def __init__(
        self,
        api_key: str,
        agent_id: str,
        model: str,
        reasoning_effort: str,
        timeout: float = 180,
        project_id: str | None = None,
        http_transport=None,
    ):
        self.api_key = api_key
        self.agent_id = agent_id
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.project_id = project_id
        self.http_transport = http_transport

    async def _request(self, client, method: str, path: str, **kwargs) -> dict:
        async with client.stream(method, API_URL + path, **kwargs) as response:
            if not response.is_success:
                raise OpenAITransportError(f"OpenAI HTTP {response.status_code}")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise OpenAITransportError("OpenAI response exceeds the size limit")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise OpenAITransportError("OpenAI returned an invalid response")
            return result

    def _verify_session(self, session: dict, agent: dict, session_id: str) -> None:
        actual = session.get("agent", {})
        if (
            session.get("id") != session_id
            or session.get("environment") != {"type": "none"}
            or session.get("vault_ids") != []
            or actual.get("id") != self.agent_id
            or actual.get("tools") != []
            or (actual.get("multi_agent") or {}).get("enabled") is not False
            or any(actual.get(key) != agent[key] for key in ("model", "instructions", "text"))
            or (actual.get("reasoning") or {}).get("effort") != self.reasoning_effort
        ):
            raise OpenAITransportError("OpenAI session configuration does not match policy")
        if session.get("required_actions") or session.get("status") not in {"idle", "in_progress"}:
            raise OpenAITransportError("OpenAI session cannot complete without external action")

    async def _page(self, client, path: str) -> list[dict]:
        page = await self._request(client, "GET", path, params={"limit": 100, "order": "asc"})
        if page.get("has_more") is not False or not isinstance(page.get("data"), list):
            raise OpenAITransportError("OpenAI result page is incomplete")
        if any(not isinstance(item, dict) for item in page["data"]):
            raise OpenAITransportError("OpenAI result page is invalid")
        return page["data"]

    async def _output(self, client, session_id: str, turn: dict) -> dict:
        items = await self._page(client, f"/agents/sessions/{session_id}/items")
        if any(item.get("type") not in {"message", "reasoning"} for item in items):
            raise OpenAITransportError("OpenAI attempted an unauthorized tool")
        finals = [
            item
            for item in items
            if item.get("role") == "assistant" and item.get("phase") == "final_answer"
        ]
        if len(finals) != 1:
            raise OpenAITransportError("OpenAI did not return one final answer")
        final = finals[0]
        if final.get("turn_id") != turn["id"] or final.get("status") != "completed":
            raise OpenAITransportError("OpenAI final answer is incomplete or unrelated")
        parts = final.get("content")
        if (
            not isinstance(parts, list)
            or not parts
            or any(
                not isinstance(part, dict)
                or part.get("type") != "output_text"
                or not isinstance(part.get("text"), str)
                for part in parts
            )
        ):
            raise OpenAITransportError("OpenAI final answer has invalid content")
        result = json.loads("".join(part["text"] for part in parts))
        if not isinstance(result, dict):
            raise OpenAITransportError("OpenAI final answer is not a JSON object")
        return result

    async def _poll(self, client, session: dict, agent: dict, session_id: str) -> tuple[dict, dict]:
        while True:
            self._verify_session(session, agent, session_id)
            turns = await self._page(client, f"/agents/sessions/{session_id}/turns")
            if len(turns) > 1:
                raise OpenAITransportError("OpenAI created unexpected additional turns")
            if turns:
                turn = turns[0]
                if turn.get("subagent_id") is not None or turn.get("session_id") != session_id:
                    raise OpenAITransportError("OpenAI returned an unrelated turn")
                resource_id(turn.get("id"), "turn")
                status = turn.get("status")
                if status == "completed":
                    return await self._output(client, session_id, turn), {
                        "provider": "openai_agents",
                        "session_id": session_id,
                        "turn_id": turn["id"],
                        "usage": token_usage(turn.get("usage")),
                    }
                if status not in {"queued", "in_progress"}:
                    raise OpenAITransportError("OpenAI turn did not complete successfully")
            await asyncio.sleep(2)
            session = await self._request(client, "GET", f"/agents/sessions/{session_id}")

    async def run(self, *, instructions: str, schema: dict, content) -> tuple[dict, dict]:
        agent = {
            "model": self.model,
            "instructions": instructions,
            "tools": [],
            "multi_agent": {"enabled": False},
            "reasoning": {"effort": self.reasoning_effort, "summary": None},
            "text": {"format": {"type": "json_schema", "schema": schema}, "verbosity": "low"},
        }
        if isinstance(content, str):
            content = [{"type": "input_text", "text": content}]
        body = json.dumps(
            {
                "agent_id": self.agent_id,
                "agent": agent,
                "environment": {"type": "none"},
                "vault_ids": [],
                "input": [{"role": "user", "content": content}],
                "stream": False,
            },
            ensure_ascii=False,
        ).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise OpenAITransportError("OpenAI request exceeds the 3.8 MB limit")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "agents=v1",
            "Content-Type": "application/json",
        }
        if self.project_id:
            headers["OpenAI-Project"] = self.project_id
        session_id = None
        async with httpx.AsyncClient(
            headers=headers,
            timeout=min(self.timeout, 30),
            follow_redirects=False,
            trust_env=False,
            transport=self.http_transport,
        ) as client:
            try:
                async with asyncio.timeout(self.timeout):
                    session = await self._request(
                        client,
                        "POST",
                        "/agents/sessions",
                        content=body,
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                    session_id = resource_id(session.get("id"), "sess")
                    return await self._poll(client, session, agent, session_id)
            except BaseException as error:
                if session_id:
                    with suppress(Exception):
                        async with asyncio.timeout(5):
                            await self._request(
                                client,
                                "POST",
                                f"/agents/sessions/{session_id}/events",
                                json={"events": [{"type": "agent.session.input.cancel"}]},
                            )
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(error, OpenAITransportError):
                    raise
                message = f"OpenAI request failed: {type(error).__name__}"
                raise OpenAITransportError(message) from None
