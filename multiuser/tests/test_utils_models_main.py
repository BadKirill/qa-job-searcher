import logging
import runpy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from qa_job_bot import main as entrypoint
from qa_job_bot.config import Settings
from qa_job_bot.models import FetchReport, Job, canonical_text, utcnow
from qa_job_bot.utils import _TextExtractor, html_to_text, parse_datetime


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("<p>QA&nbsp; engineer</p><br><div>API  testing</div>", "QA engineer\nAPI testing"),
        ("plain <strong>bold</strong> &amp; words", "plain bold & words"),
    ],
)
def test_html_to_text(value: str | None, expected: str) -> None:
    assert html_to_text(value) == expected


def test_html_parser_failure_uses_plain_text_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: _TextExtractor, value: str) -> None:
        raise ValueError("invalid parser input")

    monkeypatch.setattr(_TextExtractor, "feed", fail)
    assert html_to_text("<p>Safe &amp; usable</p>") == "Safe & usable"


@pytest.mark.parametrize("value", [None, "", "invalid", "2026-13-40"])
def test_invalid_dates_return_no_date(value: str | None) -> None:
    assert parse_datetime(value) is None


@pytest.mark.parametrize(
    "value", ["2026-10-03T10:00:00Z", "2026-10-03T10:00:00", " 2026-10-03T12:00:00+02:00 "]
)
def test_dates_are_normalized_to_utc(value: str) -> None:
    assert parse_datetime(value) == datetime(2026, 10, 3, 10, tzinfo=UTC)


def test_job_identity_is_cross_source_but_location_sensitive() -> None:
    job = Job("first", "1", "QA--Engineer", "ACME!", "Remote / Serbia", "https://a.example")
    same = replace(
        job,
        source="second",
        source_id="2",
        title="qa engineer",
        company="acme",
        location="REMOTE Serbia",
        url="https://b.example",
    )
    assert job.fingerprint == same.fingerprint
    assert len(job.fingerprint) == 64
    assert replace(job, location="Europe").fingerprint != job.fingerprint
    assert canonical_text("  QA / ТЕСТ: 123 ") == "qa тест 123"


def test_model_defaults_do_not_share_mutable_lists() -> None:
    first = Job("test", "1", "QA", "Acme", "Europe", "https://a.example")
    second = replace(first)
    other = Job("test", "2", "QA", "Acme", "Europe", "https://a.example")
    first.match_reasons.append("QA")
    assert other.match_reasons == []
    assert second.title == first.title
    report = FetchReport([], [])
    assert report.review_jobs == []
    assert abs((utcnow() - report.fetched_at).total_seconds()) < 1
    assert report.fetched_at.utcoffset() == timedelta(0)


async def test_bot_entrypoint_builds_search_and_suppresses_sensitive_http_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        "test-token",
        frozenset({1}),
        tmp_path / "db.sqlite3",
        tmp_path / "profile",
        tmp_path / "boards",
        tmp_path / "channels",
        log_level="INVALID",
    )
    monkeypatch.setattr(entrypoint.Settings, "from_env", lambda: settings)
    bot_run = AsyncMock()
    constructed = []

    class Bot:
        def __init__(self, configured, profile, service, repository) -> None:
            constructed.append((configured, profile, service, repository))

        run = bot_run

    monkeypatch.setattr(entrypoint, "TelegramBot", Bot)
    await entrypoint.async_main()
    assert constructed[0][0] == settings
    assert constructed[0][3].stats() == (0, None)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    bot_run.assert_awaited_once()


def test_sync_entrypoint_runs_async_application(monkeypatch: pytest.MonkeyPatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(entrypoint, "async_main", run)
    entrypoint.main()
    run.assert_awaited_once()


def test_sync_entrypoint_handles_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(coroutine) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint.asyncio, "run", interrupt)
    assert entrypoint.main() is None


def test_module_entrypoint_dispatches_application(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    def run(coroutine) -> None:
        called.append(coroutine.cr_code.co_name)
        coroutine.close()

    monkeypatch.setattr(entrypoint.asyncio, "run", run)
    runpy.run_path(entrypoint.__file__, run_name="__main__")
    assert called == ["async_main"]
