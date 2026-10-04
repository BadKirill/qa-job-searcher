from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from qa_job_bot.config import Board
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text


class GreenhouseSource(JobSource):
    def __init__(self, board: Board) -> None:
        self.board = board
        self.name = f"Greenhouse · {board.name}"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board.token}/jobs"
        response = await client.get(url)
        response.raise_for_status()
        items = response.json().get("jobs", [])
        quality_items = [
            item
            for item in items
            if re.search(
                r"\b(qa|sdet)\b|quality (engineer|engineering)|"
                r"test (automation |)engineer|test architect",
                str(item.get("title") or "").casefold(),
            )
        ]
        detail_results = await asyncio.gather(
            *(client.get(f"{url}/{item['id']}") for item in quality_items),
            return_exceptions=True,
        )
        details: dict[str, dict[str, object]] = {}
        for item, result in zip(quality_items, detail_results, strict=True):
            if isinstance(result, Exception) or result.is_error:
                continue
            details[str(item["id"])] = result.json()

        jobs: list[Job] = []
        for item in quality_items:
            detail = details.get(str(item["id"]))
            if detail is None:
                continue
            location = str((item.get("location") or {}).get("name") or "Not specified")
            description = html_to_text(detail.get("content"))
            workplace_text = f"{location} {description[:2500]}".casefold()
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
                    description=description,
                    published_at=published,
                    remote=workplace_type == "remote",
                    workplace_type=workplace_type,
                    tags=self.board.tags,
                )
            )
        return jobs
