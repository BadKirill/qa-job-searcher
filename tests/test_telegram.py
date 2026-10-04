from typing import Any, cast

import httpx
import pytest

from qa_job_bot.config import SearchProfile, Settings
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.repository import JobRepository
from qa_job_bot.service import JobSearchService
from qa_job_bot.telegram import TelegramAPI, TelegramBot


async def test_telegram_transport_error_does_not_expose_token() -> None:
    token = "123456:super-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    api = TelegramAPI(token, 1)
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError) as captured:
            await api.call("getMe")
        assert token not in str(captured.value)
        assert "getMe" in str(captured.value)
    finally:
        await api.close()


class FakeAPI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, chat_id: int, text: str) -> None:
        self.messages.append(text)


class CachedService:
    def __init__(self, report: FetchReport) -> None:
        self.last_report = report

    async def refresh(self) -> FetchReport:
        raise AssertionError("fresh cached report should be reused")


async def test_alerts_on_reuses_recent_search_without_repeating_jobs(tmp_path: Any) -> None:
    job = Job(
        source="test",
        source_id="1",
        title="QA Lead",
        company="Acme",
        location="United States",
        url="https://example.com/1",
        remote=True,
        score=80,
    )
    report = FetchReport(jobs=[job], review_jobs=[], errors=[])
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.upsert_jobs([job])
    settings = Settings(
        telegram_bot_token="test-token",
        allowed_user_ids=frozenset({123}),
        database_path=tmp_path / "jobs.sqlite3",
        search_profile_path=tmp_path / "profile.toml",
        job_boards_path=tmp_path / "boards.toml",
    )
    bot = TelegramBot(
        settings,
        SearchProfile(),
        cast(JobSearchService, CachedService(report)),
        repository,
    )
    await bot.api.close()
    fake_api = FakeAPI()
    bot.api = cast(TelegramAPI, fake_api)

    await bot._alerts_on(123)

    assert len(fake_api.messages) == 1
    assert "дальше пришлю только новые" in fake_api.messages[0]
    assert repository.undelivered(123, [job], 10) == []
