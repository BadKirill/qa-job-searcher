import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qa_job_bot.config import SearchProfile, Settings
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.repository import JobRepository
from qa_job_bot.telegram import HELP_TEXT, SOURCES_TEXT, TelegramAPI, TelegramBot, format_job


def vacancy(identifier: str = "1", **values) -> Job:
    return Job(
        source="test",
        source_id=identifier,
        title="QA Engineer",
        company=f"Acme {identifier}",
        location="Europe",
        url=f"https://jobs.example/{identifier}",
        **values,
    )


@pytest.fixture
async def bot(tmp_path: Path):
    settings = Settings(
        "secret-token",
        frozenset({123}),
        tmp_path / "jobs.sqlite3",
        tmp_path / "profile",
        tmp_path / "boards",
        max_alerts_per_run=2,
    )
    repository = JobRepository(settings.database_path)
    repository.initialize()
    service = SimpleNamespace(last_report=None, refresh=AsyncMock(return_value=FetchReport([], [])))
    instance = TelegramBot(settings, SearchProfile(), service, repository)
    await instance.api.close()
    instance.api = SimpleNamespace(send=AsyncMock(), call=AsyncMock(), close=AsyncMock())
    return instance


def message(
    text: object = "/help", user_id: object = 123, chat_id: object = 123, chat_type: str = "private"
) -> dict:
    return {
        "message": {
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id},
            "text": text,
        }
    }


async def test_telegram_api_success_and_send_payload() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"id": 1}}, request=request)

    api = TelegramAPI("test-token", 1)
    await api.close()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await api.call("getMe") == {"id": 1}
    await api.send(123, "hello")
    assert seen[0].url.path.endswith("/getMe")
    assert seen[1].url.path.endswith("/sendMessage")
    import json

    assert json.loads(seen[1].content) == {
        "chat_id": 123,
        "text": "hello",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    await api.close()
    assert api.client.is_closed


async def test_telegram_api_rejects_negative_application_response() -> None:
    api = TelegramAPI("test-token", 1)
    await api.close()
    api.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"ok": False, "description": "Forbidden"}, request=request
            )
        )
    )
    with pytest.raises(RuntimeError, match="Telegram API sendMessage failed: Forbidden"):
        await api.call("sendMessage")
    await api.close()


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
async def test_telegram_network_failures_redact_token(error) -> None:
    token = "123:secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise error(str(request.url), request=request)

    api = TelegramAPI(token, 1)
    await api.close()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as captured:
        await api.call("getMe")
    assert token not in str(captured.value)
    assert captured.value.__suppress_context__
    await api.close()


def test_format_job_escapes_external_html_and_formats_optional_metadata() -> None:
    job = vacancy(
        salary="<1000 EUR>",
        employment_type="B2B & full-time",
        published_at=datetime(2026, 10, 3, tzinfo=UTC),
        match_reasons=["<reason>", "second", "third", "fourth"],
        score=80,
    )
    job.title = '<img src="x">'
    job.url = 'https://jobs.example/?q="quote"&x=1'
    text = format_job(job)
    assert "<img" not in text
    assert "&lt;img" in text
    assert "B2B &amp; full-time · 03.10.2026" in text
    assert "💰 &lt;1000 EUR&gt;" in text
    assert "&lt;reason&gt;; second; third" in text
    assert "fourth" not in text
    assert "&quot;quote&quot;&amp;x=1" in text
    assert "Регион remote" in text
    simple = vacancy(location_status="allowed")
    rendered = format_job(simple)
    assert "💰" not in rendered
    assert "Регион remote" not in rendered


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"message": {}},
        message(user_id=None),
        message(chat_id="123"),
        {"message": {"chat": {"id": 123}}},
    ],
)
async def test_invalid_update_is_ignored(bot, update: dict) -> None:
    await bot._handle_update(update)
    bot.api.send.assert_not_awaited()


async def test_group_message_is_rejected_before_private_actions(bot) -> None:
    await bot._handle_update(message("/search", chat_type="group"))
    assert "в личном чате" in bot.api.send.await_args.args[1]
    bot.service.refresh.assert_not_awaited()


async def test_unauthorized_user_cannot_invoke_search(bot) -> None:
    await bot._handle_update(message("/search", user_id=999))
    bot.api.send.assert_awaited_once_with(123, "Этот бот приватный.")
    bot.service.refresh.assert_not_awaited()


@pytest.mark.parametrize("text", [None, "", "   ", "unknown", "/unrecognized"])
async def test_empty_nontext_and_unknown_commands_return_help_hint(bot, text) -> None:
    await bot._handle_update(message(text))
    bot.api.send.assert_awaited_once_with(123, "Не понял команду. Нажми /help.")


@pytest.mark.parametrize(
    "command,expected",
    [(" /HELP@mybot extra ", HELP_TEXT), ("/start", HELP_TEXT), ("/sources", SOURCES_TEXT)],
)
async def test_command_routing_handles_case_mentions_and_arguments(bot, command, expected) -> None:
    await bot._handle_update(message(command))
    bot.api.send.assert_awaited_once_with(123, expected)


async def test_empty_allowlist_allows_user_and_whoami(bot) -> None:
    from dataclasses import replace

    bot.settings = replace(bot.settings, allowed_user_ids=frozenset())
    assert bot._authorized(999)
    await bot._handle_update(message("/whoami", user_id=999))
    bot.api.send.assert_awaited_once_with(123, "Твой Telegram user ID: <code>123</code>")


async def test_settings_and_stats_empty_and_populated(bot) -> None:
    await bot._handle_update(message("/settings"))
    assert "Минимальный score: 35" in bot.api.send.await_args.args[1]
    await bot._handle_update(message("/stats"))
    assert "Последнее обновление: ещё нет" in bot.api.send.await_args.args[1]
    assert "0 точных + 0 на проверку" in bot.api.send.await_args.args[1]
    job = vacancy()
    bot.repository.upsert_jobs([job])
    bot.service.last_report = FetchReport([job], ["source"], [vacancy("2")])
    await bot._stats(123)
    text = bot.api.send.await_args.args[1]
    assert "В базе подходящих вакансий: 1" in text
    assert "1 точных + 1 на проверку" in text
    assert "Ошибок источников в последнем поиске: 1" in text


async def test_manual_search_limits_and_marks_only_shown_jobs_delivered(bot) -> None:
    from dataclasses import replace

    bot.settings = replace(bot.settings, max_search_results=1, max_review_results=1)
    exact, excluded, review = vacancy(), vacancy("2"), vacancy("3")
    report = FetchReport([exact, excluded], ["<broken>"], [review])
    bot.repository.upsert_jobs([exact, excluded, review])
    bot.service.refresh.return_value = report
    await bot._handle_update(message("/search"))
    assert bot.api.send.await_args_list[0].args[1] == "Ищу свежие вакансии…"
    assert bot.repository.undelivered(123, [exact, excluded, review], 10) == [excluded]
    texts = [call.args[1] for call in bot.api.send.await_args_list]
    assert any("Точные совпадения" in text and "1 из 2" in text for text in texts)
    assert any("Remote — проверить географию" in text for text in texts)
    assert "&lt;broken&gt;" in texts[-1]


@pytest.mark.parametrize("previous", [None, "stale"])
async def test_enable_alerts_refreshes_stale_or_missing_baseline(bot, previous) -> None:
    job = vacancy()
    bot.repository.upsert_jobs([job])
    bot.service.last_report = (
        FetchReport([], [], fetched_at=datetime.now(UTC) - timedelta(minutes=11))
        if previous
        else None
    )
    bot.service.refresh.return_value = FetchReport([job], [])
    await bot._handle_update(message("/alerts_on"))
    bot.service.refresh.assert_awaited_once()
    assert bot.repository.subscribed_chats() == [123]
    assert bot.repository.undelivered(123, [job], 10) == []
    assert "Текущие 1 вакансий" in bot.api.send.await_args.args[1]
    await bot._handle_update(message("/alerts_off"))
    assert bot.repository.subscribed_chats() == []
    assert "выключены" in bot.api.send.await_args.args[1]


@pytest.mark.parametrize("errors", [[], ["<failure>"]])
async def test_empty_report_returns_no_results_and_optional_escaped_errors(bot, errors) -> None:
    await bot._send_report(123, FetchReport([], errors), [], [], "Heading")
    text = bot.api.send.await_args.args[1]
    assert text.startswith("Подходящих вакансий пока не найдено.")
    assert ("&lt;failure&gt;" in text) == bool(errors)


async def test_review_only_report_has_one_section(bot) -> None:
    job = vacancy()
    await bot._send_report(123, FetchReport([], [], [job]), [], [job], "exact")
    assert bot.api.send.await_count == 2
    assert "Remote — проверить географию" in bot.api.send.await_args_list[0].args[1]


async def test_exact_only_report_has_no_review_section(bot) -> None:
    job = vacancy()
    await bot._send_report(123, FetchReport([job], []), [job], [], "Exact")
    assert bot.api.send.await_count == 2
    assert "Exact" in bot.api.send.await_args_list[0].args[1]


async def test_polling_advances_offset_and_dispatches_updates(bot) -> None:
    calls = 0

    async def call(method, **payload):
        nonlocal calls
        calls += 1
        assert method == "getUpdates"
        assert payload["allowed_updates"] == ["message"]
        if calls == 1:
            assert payload["offset"] == 0
            return [{"update_id": 5, **message()}, {"update_id": 4, **message("/whoami")}]
        assert payload["offset"] == 6
        bot._stop.set()
        return []

    bot.api.call.side_effect = call
    await bot._poll_loop()
    assert bot.offset == 6
    assert bot.api.send.await_count == 2


async def test_polling_recovers_errors_and_cancellation_is_propagated(bot, monkeypatch) -> None:
    bot.api.call.side_effect = RuntimeError("offline")

    async def sleep(delay):
        assert delay == 3
        bot._stop.set()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await bot._poll_loop()
    bot._stop.clear()
    bot.api.call.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await bot._poll_loop()


async def test_bot_shutdown_cancels_alerts_and_closes_api(bot, monkeypatch) -> None:
    bot.api.call.return_value = {"username": "test_bot"}
    monkeypatch.setattr(bot, "_poll_loop", AsyncMock(side_effect=RuntimeError("stop polling")))
    monkeypatch.setattr(bot, "_alert_loop", AsyncMock())
    with pytest.raises(RuntimeError, match="stop polling"):
        await bot.run()
    assert bot._stop.is_set()
    bot.api.close.assert_awaited_once()
    assert [call.args[0] for call in bot.api.call.await_args_list] == [
        "deleteWebhook",
        "setMyCommands",
        "getMe",
    ]


async def test_alert_cycles_skip_empty_subscriptions_retry_failed_delivery_and_deduplicate(
    bot,
    monkeypatch,
) -> None:
    exact, review = vacancy(), vacancy("2")
    bot.repository.upsert_jobs([exact, review])
    bot.service.refresh.return_value = FetchReport([exact], [], [review])
    cycles = 0

    async def sleep(delay):
        nonlocal cycles
        assert delay == bot.settings.search_interval_minutes * 60
        cycles += 1
        if cycles == 2:
            bot.repository.set_subscription(123, True)
            bot.repository.set_subscription(456, True)
        if cycles == 4:
            raise asyncio.CancelledError

    async def send_report(chat_id, report, jobs, review_jobs, heading):
        if chat_id == 123:
            raise RuntimeError("chat blocked bot")
        assert jobs == [exact]
        assert review_jobs == [review]
        assert heading == "Новые точные совпадения"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(bot, "_send_report", AsyncMock(side_effect=send_report))
    with pytest.raises(asyncio.CancelledError):
        await bot._alert_loop()
    assert bot.service.refresh.await_count == 2
    assert bot._send_report.await_count == 3
    assert bot.repository.undelivered(456, [exact, review], 10) == []
    assert bot.repository.undelivered(123, [exact, review], 10) == [exact, review]
