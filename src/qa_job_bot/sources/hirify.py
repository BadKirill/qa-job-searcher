from __future__ import annotations

import asyncio
from typing import Any

import httpx

from qa_job_bot.config import EUROPE_CIS_LOCATION_TERMS
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import parse_datetime


class HirifySource(JobSource):
    name = "Hirify"
    endpoint = "https://api.hirify.me/api/vacancies"
    pages = (1, 2, 3)

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        responses = await asyncio.gather(
            *(
                client.get(
                    self.endpoint,
                    params={
                        "specializations": "qa_testing",
                        "work_format": "remote",
                        "page": page,
                    },
                )
                for page in self.pages
            )
        )
        jobs: dict[str, Job] = {}
        for response in responses:
            response.raise_for_status()
            for item in response.json().get("data", []):
                if item.get("is_scam") or item.get("is_potential_scam"):
                    continue
                source_id = str(item.get("id") or item.get("slug") or "")
                if not source_id:
                    continue
                formats = [str(value).casefold() for value in item.get("work_format") or []]
                workplace = "remote" if "remote" in formats else formats[0] if formats else ""
                slug = str(item.get("slug") or source_id)
                tags = tuple(
                    str(tag.get("name") or "") for tag in item.get("tags") or [] if tag.get("name")
                )
                stack = " ".join(str(value) for value in item.get("main_stack") or [])
                description = " ".join(
                    value
                    for value in (
                        str(item.get("description") or item.get("full_description") or ""),
                        str(item.get("tldr") or ""),
                        stack,
                    )
                    if value
                )
                company = str(item.get("company_title") or "")
                if not company or company.startswith("%"):
                    company = "Company hidden"
                jobs[source_id] = Job(
                    source=self.name,
                    source_id=source_id,
                    title=str(item.get("title") or "Untitled"),
                    company=company,
                    location=self._location(item),
                    url=f"https://hirify.me/jobs/{slug}",
                    description=description,
                    published_at=parse_datetime(item.get("updated_at") or item.get("created_at")),
                    remote=workplace == "remote",
                    workplace_type=workplace,
                    employment_type=str(item.get("work_type") or ""),
                    salary=self._salary(item.get("salary")),
                    tags=tags,
                )
        return list(jobs.values())

    @classmethod
    def _location(cls, item: dict[str, Any]) -> str:
        restrictions = item.get("remote_restrictions") or item.get("allowed_locations") or []
        if restrictions:
            eligible = [str(value) for value in restrictions if cls._is_europe_or_cis(str(value))]
            values = eligible or [str(value) for value in restrictions]
            return ", ".join(cls._display_location(value) for value in values)

        remote_type = str(item.get("remote_type") or "").casefold()
        remote_types = {
            "global": "Worldwide",
            "europe": "Europe",
            "cis": "CIS",
            "russia": "Russia",
            "usa": "United States",
        }
        if remote_type:
            return remote_types.get(remote_type, cls._display_location(remote_type))

        regions = [
            str(region.get("name_en") or region.get("name") or region.get("code") or "")
            for region in item.get("regions") or []
        ]
        return ", ".join(value for value in regions if value) or "Remote"

    @staticmethod
    def _display_location(value: str) -> str:
        aliases = {
            "global": "Worldwide",
            "worldwide": "Worldwide",
            "anywhere": "Anywhere",
            "cis": "CIS",
            "usa": "United States",
            "uk": "United Kingdom",
            "russia": "Russia",
        }
        return aliases.get(value.casefold(), value.replace("_", " ").title())

    @staticmethod
    def _is_europe_or_cis(value: str) -> bool:
        normalized = value.casefold()
        region_terms = (
            "global",
            "worldwide",
            "anywhere",
            "europe",
            "european union",
            "emea",
            "cis",
            "commonwealth of independent states",
        )
        return any(term in normalized for term in (*EUROPE_CIS_LOCATION_TERMS, *region_terms))

    @staticmethod
    def _salary(value: dict[str, Any] | None) -> str:
        if not value:
            return ""
        minimum = value.get("min")
        maximum = value.get("max")
        if minimum is None and maximum is None:
            return ""
        low = f"{minimum:,.0f}" if isinstance(minimum, int | float) else "?"
        high = f"{maximum:,.0f}" if isinstance(maximum, int | float) else "?"
        currency = str(value.get("currency") or "")
        period = str(value.get("salary_period") or "")
        return f"{low}–{high} {currency}/{period}".strip("/")
