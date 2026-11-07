from __future__ import annotations

import asyncio
import logging

import httpx

from qa_job_bot.config import Board, SearchProfile
from qa_job_bot.matching import rank_jobs
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.repository import JobRepository
from qa_job_bot.sources import (
    AshbySource,
    GreenhouseSource,
    HimalayasSource,
    HirifySource,
    LeverSource,
    RemoteComSource,
    RemotiveSource,
    TelegramChannelSource,
    WeWorkRemotelySource,
)
from qa_job_bot.sources.base import JobSource

logger = logging.getLogger(__name__)


def build_sources(boards: list[Board], telegram_channels: tuple[str, ...] = ()) -> list[JobSource]:
    sources: list[JobSource] = [
        RemotiveSource(),
        HimalayasSource(),
        WeWorkRemotelySource(),
        RemoteComSource(),
        HirifySource(),
    ]
    factories = {
        "greenhouse": GreenhouseSource,
        "lever": LeverSource,
        "ashby": AshbySource,
    }
    for board in boards:
        sources.append(factories[board.kind](board))
    sources.extend(TelegramChannelSource(handle) for handle in telegram_channels)
    return sources


class JobSearchService:
    def __init__(
        self,
        profile: SearchProfile,
        sources: list[JobSource],
        repository: JobRepository,
        timeout_seconds: float,
    ) -> None:
        self.profile = profile
        self.sources = sources
        self.repository = repository
        self.timeout_seconds = timeout_seconds
        self._refresh_lock = asyncio.Lock()
        self._last_report: FetchReport | None = None

    @property
    def last_report(self) -> FetchReport | None:
        return self._last_report

    async def refresh(self) -> FetchReport:
        async with self._refresh_lock:
            headers = {"User-Agent": "QAJobSearcher/0.1 (personal Telegram bot)"}
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=headers,
                follow_redirects=True,
            ) as client:
                results = await asyncio.gather(
                    *(self._safe_fetch(source, client) for source in self.sources)
                )

            all_jobs: list[Job] = []
            errors: list[str] = []
            for jobs, error in results:
                all_jobs.extend(jobs)
                if error:
                    errors.append(error)

            ranked = rank_jobs(all_jobs, self.profile)
            self.repository.resolve_identities(ranked)
            unique = list({job.fingerprint: job for job in reversed(ranked)}.values())
            unique.sort(
                key=lambda job: (job.score, job.published_at is not None, job.published_at),
                reverse=True,
            )
            exact = [job for job in unique if job.location_status == "allowed"]
            review = [job for job in unique if job.location_status == "unknown"]
            self.repository.upsert_jobs(unique)
            self._last_report = FetchReport(jobs=exact, review_jobs=review, errors=errors)
            logger.info(
                "Fetched %d raw jobs, %d exact matches, %d review matches, %d source errors",
                len(all_jobs),
                len(exact),
                len(review),
                len(errors),
            )
            return self._last_report

    async def _safe_fetch(
        self, source: JobSource, client: httpx.AsyncClient
    ) -> tuple[list[Job], str | None]:
        try:
            return await source.fetch(client), None
        except Exception as exc:
            logger.warning("Source %s failed: %s", source.name, exc)
            return [], f"{source.name}: {type(exc).__name__}"
