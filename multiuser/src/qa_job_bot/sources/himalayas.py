from __future__ import annotations

from datetime import UTC, datetime

import httpx

from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource, collect_responses, finish_fetch
from qa_job_bot.utils import html_to_text


class HimalayasSource(JobSource):
    name = "Himalayas"
    endpoint = "https://himalayas.app/jobs/api/search"

    def __init__(self, queries=("",)):
        self.queries = tuple(queries[:8]) or ("",)

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        responses, failures = await collect_responses(
            [client.get(self.endpoint, params={"q": query, "page": 1}) for query in self.queries]
        )
        jobs: dict[str, Job] = {}
        for response in responses:
            try:
                data = response.json()
            except ValueError as error:
                failures.append(error)
                continue
            for item in data.get("jobs", []):
                url = str(item.get("applicationLink") or item.get("guid") or "")
                source_id = str(item.get("guid") or url or item.get("title"))
                restrictions = [str(value) for value in item.get("locationRestrictions") or []]
                location = ", ".join(restrictions) or "Worldwide"
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
                    location_restrictions=tuple(restrictions),
                    url=url,
                    description=html_to_text(item.get("description") or item.get("excerpt")),
                    published_at=published,
                    remote=True,
                    workplace_type="remote",
                    employment_type=str(item.get("employmentType") or ""),
                    salary=salary,
                    tags=tags,
                )
        return finish_fetch(list(jobs.values()), failures)
