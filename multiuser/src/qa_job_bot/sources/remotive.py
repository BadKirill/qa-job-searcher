from __future__ import annotations

import httpx

from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text, parse_datetime


class RemotiveSource(JobSource):
    name = "Remotive"
    endpoint = "https://remotive.com/api/remote-jobs"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        response = await client.get(self.endpoint)
        response.raise_for_status()
        jobs: list[Job] = []
        for item in response.json().get("jobs", []):
            tags = tuple(str(tag) for tag in item.get("tags") or [])
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]),
                    title=str(item.get("title") or "Untitled"),
                    company=str(item.get("company_name") or "Unknown company"),
                    location=str(item.get("candidate_required_location") or "Remote"),
                    url=str(item.get("url") or ""),
                    description=html_to_text(item.get("description")),
                    published_at=parse_datetime(item.get("publication_date")),
                    remote=True,
                    workplace_type="remote",
                    employment_type=str(item.get("job_type") or ""),
                    salary=str(item.get("salary") or ""),
                    tags=tags,
                )
            )
        return jobs
