from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from qa_job_bot.config import EUROPE_CIS_LOCATION_TERMS
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text


class HimalayasSource(JobSource):
    name = "Himalayas"
    endpoint = "https://himalayas.app/jobs/api/search"
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
            *(client.get(self.endpoint, params={"q": query, "page": 1}) for query in self.queries)
        )
        jobs: dict[str, Job] = {}
        for response in responses:
            response.raise_for_status()
            for item in response.json().get("jobs", []):
                url = str(item.get("applicationLink") or item.get("guid") or "")
                source_id = str(item.get("guid") or url or item.get("title"))
                restrictions = [str(value) for value in item.get("locationRestrictions") or []]
                eligible_restrictions = [
                    value for value in restrictions if self._is_europe_or_cis(value)
                ]
                if restrictions and not eligible_restrictions:
                    continue
                location = ", ".join(eligible_restrictions) or "Worldwide"
                min_salary = item.get("minSalary")
                max_salary = item.get("maxSalary")
                salary = ""
                if min_salary is not None or max_salary is not None:
                    low = f"{min_salary:,.0f}" if min_salary is not None else "?"
                    high = f"{max_salary:,.0f}" if max_salary is not None else "?"
                    salary = (
                        f"{low}–{high} {item.get('currency') or ''}/"
                        f"{item.get('salaryPeriod') or ''}"
                    ).strip("/")
                published = None
                if isinstance(item.get("pubDate"), int | float):
                    published = datetime.fromtimestamp(item["pubDate"], tz=UTC)
                tags = tuple(
                    [*(str(value) for value in item.get("categories") or [])]
                    + [*(str(value) for value in item.get("parentCategories") or [])]
                )
                jobs[source_id] = Job(
                    source=self.name,
                    source_id=source_id,
                    title=str(item.get("title") or "Untitled"),
                    company=str(item.get("companyName") or "Unknown company"),
                    location=location,
                    url=url,
                    description=html_to_text(item.get("description") or item.get("excerpt")),
                    published_at=published,
                    remote=True,
                    workplace_type="remote",
                    employment_type=str(item.get("employmentType") or ""),
                    salary=salary,
                    tags=tags,
                )
        return list(jobs.values())

    @staticmethod
    def _is_europe_or_cis(value: str) -> bool:
        normalized = value.casefold()
        region_terms = (
            "europe",
            "european union",
            "emea",
            "cis",
            "commonwealth of independent states",
        )
        return any(term in normalized for term in (*EUROPE_CIS_LOCATION_TERMS, *region_terms))
