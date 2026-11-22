from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC
from email.utils import parsedate_to_datetime

import httpx

from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource
from qa_job_bot.utils import html_to_text


class WeWorkRemotelySource(JobSource):
    name = "We Work Remotely"
    endpoint = "https://weworkremotely.com/remote-jobs.rss"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        response = await client.get(self.endpoint)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        jobs: list[Job] = []
        for item in root.findall("./channel/item"):
            raw_title = item.findtext("title", default="Untitled")
            company, separator, title = raw_title.partition(":")
            if not separator:
                title, company = raw_title, "Unknown company"
            region = item.findtext("region", default="")
            country = item.findtext("country", default="")
            location = region or country or "Remote"
            link = item.findtext("link", default="")
            published = None
            if raw_date := item.findtext("pubDate"):
                published = parsedate_to_datetime(raw_date).astimezone(UTC)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=item.findtext("guid", default=link),
                    title=title.strip(),
                    company=company.strip(),
                    location=location,
                    url=link,
                    description=html_to_text(item.findtext("description", default="")),
                    published_at=published,
                    remote=True,
                    workplace_type="remote",
                    employment_type=item.findtext("type", default=""),
                    tags=(
                        item.findtext("category", default=""),
                        item.findtext("skills", default=""),
                    ),
                )
            )
        return jobs
