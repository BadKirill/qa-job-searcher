from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from qa_job_bot.config import Board
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource, finish_fetch
from qa_job_bot.utils import html_to_text


class GreenhouseSource(JobSource):
    def __init__(self, board: Board) -> None:
        self.board = board
        self.name = f"Greenhouse · {board.name}"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board.token}/jobs"
        response = await client.get(url, params={"content": "true"})
        response.raise_for_status()
        items = response.json().get("jobs", [])
        detail_items = [
            item
            for item in items
            if str(item.get("id", "")).isdecimal() and len(str(item["id"])) <= 30
        ]
        jobs: list[Job] = []
        failures = []
        for item in detail_items:
            if not isinstance(item.get("content"), str):
                failures.append(ValueError("missing_description"))
                continue
            location = str((item.get("location") or {}).get("name") or "Not specified")
            description = html_to_text(item.get("content"))
            workplace_text = f"{location} {description}".casefold()
            workplace_type = ""
            if "hybrid" in workplace_text:
                workplace_type = "hybrid"
            elif (
                re.search(
                    r"\b(fully remote|remote role|remote position|remote-first|100% remote)\b",
                    workplace_text,
                )
                or "remote" in location.casefold()
            ):
                workplace_type = "remote"
            updated_at = item.get("updated_at")
            published = None
            if updated_at:
                with suppress(ValueError):
                    published = datetime.fromisoformat(
                        updated_at.replace("Z", "+00:00")
                    ).astimezone(UTC)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]),
                    title=str(item.get("title") or "Untitled"),
                    company=self.board.name,
                    location=location,
                    url=str(item.get("absolute_url") or ""),
                    description_quality="full",
                    description=description,
                    published_at=published,
                    remote=workplace_type == "remote",
                    workplace_type=workplace_type,
                    tags=self.board.tags,
                )
            )
        return finish_fetch(jobs, failures)
