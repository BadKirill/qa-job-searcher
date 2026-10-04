from __future__ import annotations

from typing import Any

import httpx

from qa_job_bot.config import Board
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text


class LeverSource(JobSource):
    def __init__(self, board: Board) -> None:
        self.board = board
        self.name = f"Lever · {board.name}"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        host = "api.eu.lever.co" if self.board.region == "eu" else "api.lever.co"
        url = f"https://{host}/v0/postings/{self.board.token}"
        response = await client.get(url, params={"mode": "json"})
        response.raise_for_status()
        jobs: list[Job] = []
        for item in response.json():
            categories = item.get("categories") or {}
            locations = categories.get("allLocations") or []
            location = ", ".join(locations) or str(categories.get("location") or "Not specified")
            workplace = str(item.get("workplaceType") or "").casefold()
            salary_data = item.get("salaryRange") or {}
            salary = ""
            if salary_data.get("min") is not None and salary_data.get("max") is not None:
                salary = (
                    f"{salary_data['min']:,.0f}–{salary_data['max']:,.0f} "
                    f"{salary_data.get('currency', '')}/{salary_data.get('interval', '')}"
                ).strip("/")
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]),
                    title=str(item.get("text") or "Untitled"),
                    company=self.board.name,
                    location=location,
                    url=str(item.get("hostedUrl") or item.get("applyUrl") or ""),
                    description=self._description(item),
                    remote=workplace == "remote" or "remote" in location.casefold(),
                    workplace_type=workplace,
                    employment_type=str(categories.get("commitment") or ""),
                    salary=salary,
                    tags=self.board.tags,
                )
            )
        return jobs

    @staticmethod
    def _description(item: dict[str, Any]) -> str:
        parts = [html_to_text(item.get("descriptionPlain") or item.get("description"))]
        for section in item.get("lists") or []:
            parts.extend((html_to_text(section.get("text")), html_to_text(section.get("content"))))
        parts.extend(
            (
                html_to_text(item.get("additionalPlain") or item.get("additional")),
                html_to_text(item.get("salaryDescriptionPlain") or item.get("salaryDescription")),
            )
        )
        return "\n".join(part for part in parts if part)
