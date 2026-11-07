from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text, parse_datetime


class RemoteComSource(JobSource):
    name = "Remote.com"
    endpoint = "https://talent-api.remote.com/api/v1/public/jobs"
    queries = (
        "qa engineer",
        "manual qa",
        "full stack qa",
        "qa lead",
        "sdet",
        "quality engineer",
        "test automation",
        "quality engineering manager",
    )

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        responses = await asyncio.gather(
            *(
                client.get(self.endpoint, params={"query": query, "page_size": 100})
                for query in self.queries
            )
        )
        items: dict[str, dict[str, Any]] = {}
        for response in responses:
            response.raise_for_status()
            for item in response.json().get("data", {}).get("jobs", []):
                company = item.get("company_profile") or {}
                key = f"{company.get('slug', '')}/{item.get('slug', '')}"
                if key != "/":
                    items[key] = item

        detail_items = [
            (key, item)
            for key, item in items.items()
            if self._looks_like_qa_title(str(item.get("title") or ""))
        ]
        detail_results = await asyncio.gather(
            *(client.get(f"{self.endpoint}/{key}") for key, _ in detail_items),
            return_exceptions=True,
        )
        descriptions: dict[str, str] = {}
        for (key, _), result in zip(detail_items, detail_results, strict=True):
            if isinstance(result, Exception) or result.is_error:
                continue
            detail = result.json().get("data", {})
            descriptions[key] = html_to_text(detail.get("description"))

        jobs: list[Job] = []
        for key, item in detail_items:
            if key not in descriptions:
                continue
            company = item.get("company_profile") or {}
            workplace = str((item.get("workplace_location") or {}).get("type") or "")
            company_slug, job_slug = key.split("/", 1)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=key,
                    title=str(item.get("title") or "Untitled"),
                    company=str(company.get("name") or "Unknown company"),
                    location=self._location(item),
                    url=f"https://remote.com/jobs/{company_slug}/{job_slug}",
                    description=descriptions.get(key, ""),
                    published_at=parse_datetime(item.get("published_at")),
                    remote=workplace == "remote",
                    workplace_type=workplace,
                    employment_type=str(item.get("employment_type") or "").replace("_", " "),
                    salary=self._salary(item.get("compensation") or {}),
                    tags=tuple(
                        value
                        for value in (
                            str((item.get("department") or {}).get("name") or ""),
                            *(str(entry) for entry in item.get("seniority") or []),
                        )
                        if value
                    ),
                )
            )
        return jobs

    @staticmethod
    def _looks_like_qa_title(title: str) -> bool:
        return bool(
            re.search(
                r"\b(qa|sdet)\b|quality (engineer|engineering|assurance)|"
                r"test (automation |)engineer|automation test engineer|test architect",
                title.casefold(),
            )
        )

    @staticmethod
    def _location(item: dict[str, Any]) -> str:
        hiring = item.get("hiring_location") or {}
        if hiring.get("type") == "global":
            return "Worldwide"
        names: list[str] = []
        for entry in hiring.get("included_locations") or []:
            value = entry.get("value") or {}
            name = str(value.get("name") or value.get("code") or "")
            if name and name not in names:
                names.append(name)
        if names:
            return ", ".join(names)
        timezone = hiring.get("timezone") or hiring.get("timezone_range")
        return str(timezone or "Remote")

    @staticmethod
    def _salary(compensation: dict[str, Any]) -> str:
        minimum = compensation.get("minimum")
        maximum = compensation.get("maximum")
        if minimum is None and maximum is None:
            return ""
        low = f"{minimum / 100:,.0f}" if isinstance(minimum, int | float) else "?"
        high = f"{maximum / 100:,.0f}" if isinstance(maximum, int | float) else "?"
        currency = str((compensation.get("currency") or {}).get("code") or "")
        frequency = str(compensation.get("frequency") or "")
        return f"{low}–{high} {currency}/{frequency}".strip("/")
