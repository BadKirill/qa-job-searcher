from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from qa_job_bot.config import Settings, load_boards, load_profile, load_telegram_channels
from qa_job_bot.repository import JobRepository
from qa_job_bot.service import JobSearchService, build_sources
from qa_job_bot.telegram import TelegramBot


async def async_main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not settings.allowed_user_ids:
        logging.warning("TELEGRAM_ALLOWED_USER_IDS is empty: anyone who finds the bot can use it")
    profile = load_profile(settings.search_profile_path)
    boards = load_boards(settings.job_boards_path)
    telegram_channels = load_telegram_channels(settings.telegram_channels_path)
    repository = JobRepository(settings.database_path)
    repository.initialize()
    service = JobSearchService(
        profile=profile,
        sources=build_sources(boards, telegram_channels),
        repository=repository,
        timeout_seconds=settings.http_timeout_seconds,
    )
    bot = TelegramBot(settings, profile, service, repository)
    await bot.run()


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
