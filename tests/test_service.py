import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from qa_job_bot.config import Board, SearchProfile
from qa_job_bot.models import Job
from qa_job_bot.repository import JobRepository
from qa_job_bot.service import JobSearchService, build_sources
from qa_job_bot.sources.base import JobSource


class StubSource(JobSource):
    name = "Stub"

    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = jobs

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        return self.jobs


async def test_service_keeps_region_wide_remote_and_serbia_hybrid(tmp_path: Path) -> None:
    exact = Job(
        source="Stub",
        source_id="1",
        title="QA Lead",
        company="Acme Serbia",
        location="Belgrade, Serbia",
        url="https://example.com/1",
        remote=False,
        workplace_type="hybrid",
    )
    europe_remote = Job(
        source="Stub",
        source_id="2",
        title="Senior SDET",
        company="Acme Poland",
        location="Poland",
        url="https://example.com/2",
        remote=True,
        workplace_type="remote",
    )
    excluded = Job(
        source="Stub",
        source_id="3",
        title="Senior QA Engineer",
        company="Acme Brazil",
        location="Brazil",
        url="https://example.com/3",
        remote=True,
        workplace_type="remote",
    )
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    service = JobSearchService(
        SearchProfile(),
        [StubSource([exact, europe_remote, excluded])],
        repository,
        timeout_seconds=1,
    )

    report = await service.refresh()

    assert {job.source_id for job in report.jobs} == {"1"}
    assert report.review_jobs == []


def test_source_factory_includes_all_configured_boards_and_channels() -> None:
    boards = [Board(kind, kind.title(), "company") for kind in ("greenhouse", "lever", "ashby")]
    sources = build_sources(boards, ("qa_jobs", "qa_remote"))
    assert len(sources) == 10
    assert [source.name for source in sources[-5:]] == [
        "Greenhouse · Greenhouse",
        "Lever · Lever",
        "Ashby · Ashby",
        "Telegram · @qa_jobs",
        "Telegram · @qa_remote",
    ]


async def test_partial_source_failure_preserves_valid_results_and_review_queue(
    tmp_path: Path,
) -> None:
    job = Job("good", "1", "QA Engineer", "Acme", "Europe", "https://jobs.example/1", remote=True)
    duplicate = replace(
        job, source="better", source_id="2", salary="EUR 4000", description="Playwright Python"
    )
    unknown = replace(job, source_id="3", company="Another", location="Remote")

    class FailedSource(JobSource):
        name = "Failed"

        async def fetch(self, client):
            raise httpx.ReadTimeout("unavailable")

    repository = JobRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    service = JobSearchService(
        replace(SearchProfile(), allow_unknown_remote_location=True),
        [StubSource([job, duplicate, unknown]), FailedSource()],
        repository,
        1,
    )
    assert service.last_report is None
    report = await service.refresh()
    assert report.jobs == [duplicate]
    assert report.review_jobs == [unknown]
    assert report.errors == ["Failed: ReadTimeout"]
    assert service.last_report is report
    assert repository.stats()[0] == 2


async def test_empty_sources_produce_valid_empty_report(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    service = JobSearchService(SearchProfile(), [], repository, 1)
    report = await service.refresh()
    assert report.jobs == report.review_jobs == report.errors == []
    assert repository.stats() == (0, None)


async def test_service_keeps_distinct_roles_with_identical_title_company_and_location(
    tmp_path: Path,
) -> None:
    first = Job(
        "board", "one", "QA Engineer", "Acme", "Europe", "https://jobs.test/one", remote=True
    )
    second = replace(first, source_id="two", url="https://jobs.test/two")
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    service = JobSearchService(SearchProfile(), [StubSource([first, second])], repository, 1)

    report = await service.refresh()

    assert len(report.jobs) == 2
    assert first.fingerprint != second.fingerprint
    assert repository.stats()[0] == 2


async def test_refresh_lock_prevents_overlapping_fetches(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    calls = 0

    class CountingSource(JobSource):
        name = "Counting"

        async def fetch(self, client):
            nonlocal active, maximum, calls
            active += 1
            calls += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return []

    repository = JobRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    service = JobSearchService(SearchProfile(), [CountingSource()], repository, 1)
    await asyncio.gather(service.refresh(), service.refresh())
    assert maximum == 1
    assert calls == 2


async def test_fetch_cancellation_is_not_disguised_as_source_failure(tmp_path: Path) -> None:
    class CancelledSource(JobSource):
        name = "Cancelled"

        async def fetch(self, client):
            raise asyncio.CancelledError

    repository = JobRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    service = JobSearchService(SearchProfile(), [CancelledSource()], repository, 1)
    with pytest.raises(asyncio.CancelledError):
        await service.refresh()
    assert service.last_report is None
