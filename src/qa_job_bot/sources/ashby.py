from __future__ import annotations

import httpx

from qa_job_bot.config import Board
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text, parse_datetime


class AshbySource(JobSource):
    def __init__(self, board: Board) -> None:
        self.board = board
        self.name = f"Ashby · {board.name}"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.board.token}"
        response = await client.get(url, params={"includeCompensation": "true"})
        response.raise_for_status()
        jobs: list[Job] = []
        for item in response.json().get("jobs", []):
            if item.get("isListed") is False:
                continue
            secondary = [
                str(value.get("location"))
                for value in (item.get("secondaryLocations") or [])
                if value.get("location")
            ]
            primary = str(item.get("location") or "Not specified")
            location = ", ".join(dict.fromkeys([primary, *secondary]))
            compensation = item.get("compensation") or {}
            salary = str(compensation.get("compensationTierSummary") or "")
            workplace = str(item.get("workplaceType") or "").casefold()
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]),
                    title=str(item.get("title") or "Untitled"),
                    company=self.board.name,
                    location=location,
                    url=str(item.get("jobUrl") or item.get("applyUrl") or ""),
                    description=html_to_text(
                        item.get("descriptionPlain") or item.get("descriptionHtml")
                    ),
                    published_at=parse_datetime(item.get("publishedAt")),
                    remote=bool(item.get("isRemote")) or workplace == "remote",
                    workplace_type=workplace,
                    employment_type=str(item.get("employmentType") or ""),
                    salary=salary,
                    tags=self.board.tags,
                )
            )
        return jobs
