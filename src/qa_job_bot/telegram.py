from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from html import escape
from typing import Any

import httpx

from qa_job_bot.config import SearchProfile, Settings
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.repository import JobRepository
from qa_job_bot.service import JobSearchService

logger = logging.getLogger(__name__)

HELP_TEXT = """<b>QA Job Searcher</b>

/search — найти подходящие вакансии сейчас
/alerts_on — включить автоматические уведомления
/alerts_off — выключить уведомления
/settings — показать активные критерии
/sources — источники и полезные ручные ссылки
/stats — статистика локальной базы
/whoami — показать твой Telegram user ID
/help — эта справка

Бот только находит и ранжирует вакансии. Отклик всегда остаётся под твоим контролем."""

SOURCES_TEXT = """<b>Автоматические источники</b>
• Remotive
• Himalayas
• We Work Remotely
• Remote.com
• Hirify
• 13 публичных Telegram-каналов из <code>config/telegram_channels.toml</code>
• карьерные страницы на Greenhouse, Lever и Ashby из <code>config/job_boards.toml</code>

<b>Ручной поиск</b>
• <a href="https://wellfound.com/jobs/">Wellfound</a>
• <a href="https://landing.jobs/">Landing.jobs</a>
• <a href="https://nofluffjobs.com/">No Fluff Jobs</a>
• <a href="https://cryptojobslist.com/">CryptoJobsList</a>
• <a href="https://www.ycombinator.com/jobs?location=remote">Y Combinator Jobs</a>
• <a href="https://helloworld.rs/oglasi-za-posao/qa">HelloWorld.rs</a>
• <a href="https://poslovi.infostud.com/">Infostud</a>
• <a href="https://www.instahyre.com/candidate/opportunities/">Instahyre (нужен вход)</a>"""


class TelegramAPI:
    def __init__(self, token: str, timeout_seconds: float) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, read=40.0))

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, **payload: Any) -> Any:
        try:
            response = await self.client.post(f"{self.base_url}/{method}", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Telegram API {method} transport failure: {type(exc).__name__}"
            ) from None
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {body.get('description')}")
        return body.get("result")

    async def send(self, chat_id: int, text: str) -> None:
        await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


def format_job(job: Job) -> str:
    date = ""
    if job.published_at:
        date = job.published_at.astimezone(UTC).strftime("%d.%m.%Y")
    metadata = [job.location]
    if job.employment_type:
        metadata.append(job.employment_type)
    if date:
        metadata.append(date)
    salary = f"\n💰 {escape(job.salary)}" if job.salary else ""
    reasons = "; ".join(job.match_reasons[:3])
    review_notice = (
        "\n⚠️ Регион remote не указан однозначно — проверь условия."
        if job.location_status == "unknown"
        else ""
    )
    return (
        f"<b>{escape(job.title)}</b>\n"
        f"{escape(job.company)}\n"
        f"📍 {escape(' · '.join(metadata))}{salary}\n"
        f"🎯 {job.score}/100 — {escape(reasons)}\n"
        f"Источник: {escape(job.source)}{review_notice}\n"
        f'<a href="{escape(job.url, quote=True)}">Открыть вакансию</a>'
    )


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        profile: SearchProfile,
        service: JobSearchService,
        repository: JobRepository,
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.service = service
        self.repository = repository
        self.api = TelegramAPI(settings.telegram_bot_token, settings.http_timeout_seconds)
        self.offset = 0
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self.api.call("deleteWebhook", drop_pending_updates=False)
        await self.api.call(
            "setMyCommands",
            commands=[
                {"command": "search", "description": "Найти вакансии сейчас"},
                {"command": "alerts_on", "description": "Включить автоуведомления"},
                {"command": "alerts_off", "description": "Выключить автоуведомления"},
                {"command": "settings", "description": "Показать критерии поиска"},
                {"command": "sources", "description": "Показать источники"},
                {"command": "stats", "description": "Статистика базы"},
                {"command": "whoami", "description": "Показать Telegram user ID"},
                {"command": "help", "description": "Справка"},
            ],
        )
        me = await self.api.call("getMe")
        logger.info("Started Telegram bot @%s", me.get("username"))
        alert_task = asyncio.create_task(self._alert_loop(), name="job-alert-loop")
        try:
            await self._poll_loop()
        finally:
            self._stop.set()
            alert_task.cancel()
            await asyncio.gather(alert_task, return_exceptions=True)
            await self.api.close()

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self.api.call(
                    "getUpdates",
                    offset=self.offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling failed")
                await asyncio.sleep(3)

    def _authorized(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_user_ids

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        if chat.get("type") != "private":
            await self.api.send(chat_id, "Используй этого персонального бота в личном чате.")
            return
        if not self._authorized(user_id):
            logger.warning("Rejected Telegram user %s", user_id)
            await self.api.send(chat_id, "Этот бот приватный.")
            return

        text = str(message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold() if text else ""
        handlers = {
            "/start": self._help,
            "/help": self._help,
            "/search": self._search,
            "/alerts_on": self._alerts_on,
            "/alerts_off": self._alerts_off,
            "/settings": self._settings,
            "/sources": self._sources,
            "/stats": self._stats,
            "/whoami": self._whoami,
        }
        handler = handlers.get(command)
        if handler:
            await handler(chat_id)
        else:
            await self.api.send(chat_id, "Не понял команду. Нажми /help.")

    async def _help(self, chat_id: int) -> None:
        await self.api.send(chat_id, HELP_TEXT)

    async def _sources(self, chat_id: int) -> None:
        await self.api.send(chat_id, SOURCES_TEXT)

    async def _search(self, chat_id: int) -> None:
        await self.api.send(chat_id, "Ищу свежие вакансии…")
        report = await self.service.refresh()
        jobs = report.jobs[: self.settings.max_search_results]
        review_jobs = report.review_jobs[: self.settings.max_review_results]
        await self._send_report(chat_id, report, jobs, review_jobs, "Точные совпадения")
        self.repository.mark_delivered(chat_id, [*jobs, *review_jobs])

    async def _alerts_on(self, chat_id: int) -> None:
        self.repository.set_subscription(chat_id, True)
        report = self.service.last_report
        cache_is_fresh = bool(
            report and (datetime.now(UTC) - report.fetched_at).total_seconds() < 600
        )
        if not cache_is_fresh:
            await self.api.send(chat_id, "Включаю уведомления и фиксирую текущую выдачу…")
            report = await self.service.refresh()
        assert report is not None
        baseline = [*report.jobs, *report.review_jobs]
        self.repository.mark_delivered(chat_id, baseline)
        await self.api.send(
            chat_id,
            "Автоуведомления включены. "
            f"Текущие {len(baseline)} вакансий приняты за базовую подборку; "
            "дальше пришлю только новые.",
        )

    async def _alerts_off(self, chat_id: int) -> None:
        self.repository.set_subscription(chat_id, False)
        await self.api.send(
            chat_id, "Автоуведомления выключены. Ручной /search продолжит работать."
        )

    async def _settings(self, chat_id: int) -> None:
        roles = ", ".join(self.profile.role_keywords[:8])
        text = (
            "<b>Активный профиль</b>\n"
            f"Минимальный score: {self.profile.min_score}\n"
            "Формат: Remote; Hybrid — только Serbia\n"
            "География: Europe и CIS (включая Worldwide)\n"
            f"Основные роли: {escape(roles)}…\n\n"
            "Полный профиль редактируется в <code>config/search_profile.toml</code>."
        )
        await self.api.send(chat_id, text)

    async def _stats(self, chat_id: int) -> None:
        count, last_seen = self.repository.stats()
        last = last_seen.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC") if last_seen else "ещё нет"
        errors = len(self.service.last_report.errors) if self.service.last_report else 0
        current = len(self.service.last_report.jobs) if self.service.last_report else 0
        review = len(self.service.last_report.review_jobs) if self.service.last_report else 0
        await self.api.send(
            chat_id,
            f"В базе подходящих вакансий: {count}\nПоследнее обновление: {last}\n"
            f"Текущая выдача: {current} точных + {review} на проверку\n"
            f"Ошибок источников в последнем поиске: {errors}",
        )

    async def _whoami(self, chat_id: int) -> None:
        await self.api.send(chat_id, f"Твой Telegram user ID: <code>{chat_id}</code>")

    async def _send_report(
        self,
        chat_id: int,
        report: FetchReport,
        jobs: list[Job],
        review_jobs: list[Job],
        heading: str,
    ) -> None:
        if not jobs and not review_jobs:
            suffix = (
                f" Источники с ошибками: {escape(', '.join(report.errors))}."
                if report.errors
                else ""
            )
            await self.api.send(chat_id, f"Подходящих вакансий пока не найдено.{suffix}")
            return
        if jobs:
            await self._send_section(chat_id, heading, jobs, len(report.jobs))
        if review_jobs:
            await self._send_section(
                chat_id,
                "Remote — проверить географию",
                review_jobs,
                len(report.review_jobs),
            )
        if report.errors:
            await self.api.send(
                chat_id,
                "Часть источников временно недоступна: " + escape(", ".join(report.errors)),
            )

    async def _send_section(self, chat_id: int, heading: str, jobs: list[Job], total: int) -> None:
        await self.api.send(
            chat_id,
            f"<b>{escape(heading)}</b> · показано {len(jobs)} из {total}",
        )
        for job in jobs:
            await self.api.send(chat_id, format_job(job))

    async def _alert_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.search_interval_minutes * 60)
            chats = self.repository.subscribed_chats()
            if not chats:
                continue
            report = await self.service.refresh()
            for chat_id in chats:
                exact = self.repository.undelivered(
                    chat_id, report.jobs, self.settings.max_alerts_per_run
                )
                remaining = self.settings.max_alerts_per_run - len(exact)
                review = self.repository.undelivered(chat_id, report.review_jobs, remaining)
                if not exact and not review:
                    continue
                try:
                    await self._send_report(
                        chat_id, report, exact, review, "Новые точные совпадения"
                    )
                    self.repository.mark_delivered(chat_id, [*exact, *review])
                except Exception:
                    logger.exception("Could not deliver alerts to chat %s", chat_id)
