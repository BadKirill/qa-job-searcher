import argparse
import asyncio
import json
import logging
import re
import signal
from contextlib import contextmanager, suppress
from html import escape
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.accounts import normalize_login
from qa_job_bot.maintenance import active as maintenance_active
from qa_job_bot.openai_config import private_text
from qa_job_bot.operations import ready, supervised
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.telegram import TelegramAPI
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError

logger = logging.getLogger(__name__)


@contextmanager
def singleton(database):
    with database.engine.connect() as connection:
        if not connection.execute(text("SELECT pg_try_advisory_lock(1853392759)")).scalar_one():
            raise RuntimeError("Another personal Telegram polling process is running")
        try:
            yield connection
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(1853392759)"))
            except Exception:
                connection.invalidate()


def bind(database, chat_id, login, enabled=None):
    if type(chat_id) is not int or not 0 < chat_id < 2**63:
        raise StoreError("Нужен ID личного Telegram-чата.", 422)
    with database.transaction() as connection:
        user = (
            connection.execute(
                select(s.users)
                .where(
                    s.users.c.login == normalize_login(login),
                    s.users.c.disabled.is_(False),
                    s.users.c.password_hash.is_not(None),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if user is None:
            raise StoreError("Активный аккаунт не найден.", 404)
        statement = insert(s.telegram_bindings).values(
            chat_id=chat_id, user_id=user["id"], enabled=False if enabled is None else enabled
        )
        connection.execute(statement.on_conflict_do_nothing())
        row = (
            connection.execute(
                select(s.telegram_bindings)
                .where(s.telegram_bindings.c.chat_id == chat_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if row["user_id"] != user["id"]:
            raise StoreError("Чат уже привязан к другому аккаунту.", 409)
        if enabled is not None:
            connection.execute(
                s.telegram_bindings.update()
                .where(s.telegram_bindings.c.chat_id == chat_id)
                .values(enabled=enabled)
            )
    return {"bound": True, "alerts": row["enabled"] if enabled is None else enabled}


class PersonalTelegram:
    def __init__(self, database, api, settings=None):
        self.database, self.api, self.lock = database, api, None
        self.settings = settings or WebSettings.from_env()

    def owner(self, chat_id):
        with self.database.transaction() as connection:
            return connection.execute(
                select(s.telegram_bindings.c.user_id)
                .join(s.users)
                .where(
                    s.telegram_bindings.c.chat_id == chat_id,
                    s.users.c.disabled.is_(False),
                    s.users.c.password_hash.is_not(None),
                )
            ).scalar_one_or_none()

    async def send(self, chat_id, message, user_id=None):
        if self.lock is not None:
            self.lock.execute(text("SELECT 1"))
        if user_id is not None and self.owner(chat_id) != user_id:
            raise StoreError("Telegram-привязка недоступна.", 403)
        await self.api.send(chat_id, message)

    def acknowledge(self, store, chat_id, identifiers):
        with store.write() as connection:
            for job_id in identifiers:
                connection.execute(
                    insert(s.deliveries)
                    .values(user_id=store.user_id, chat_id=chat_id, job_id=job_id)
                    .on_conflict_do_nothing()
                )

    async def handle(self, update):
        if maintenance_active(self.settings.data_dir):
            return
        message = update.get("message") or {}
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        chat_id = chat.get("id")
        if (
            type(chat_id) is not int
            or chat_id <= 0
            or chat.get("type") != "private"
            or type(sender.get("id")) is not int
            or sender.get("id") != chat_id
        ):
            return
        command = str(message.get("text") or "").strip().split(maxsplit=1)
        command = command[0].split("@", 1)[0].casefold() if command else ""
        if command == "/whoami":
            await self.send(chat_id, f"Ваш Telegram ID: <code>{chat_id}</code>")
            return
        owner = self.owner(chat_id)
        if owner is None:
            await self.send(
                chat_id, "Личный кабинет не привязан. Передайте администратору ID из /whoami."
            )
            return
        store = PostgresStore(self.database, owner)
        try:
            if command == "/search":
                task = store.enqueue_search("telegram:" + str(update["update_id"]))
                with store.write() as connection:
                    statement = insert(s.runtime_control).values(
                        key=f"telegram-pending:{chat_id}",
                        payload={
                            "owner_id": owner,
                            "chat_id": chat_id,
                            "task_id": task["id"],
                        },
                    )
                    connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=[s.runtime_control.c.key],
                            set_={"payload": statement.excluded.payload},
                        )
                    )
                response = "Личный поиск поставлен в очередь. Результаты доступны в кабинете."
            elif command in {"/alerts_on", "/alerts_off"}:
                enabled = command == "/alerts_on"
                if enabled:
                    self.acknowledge(store, chat_id, store.get_setting("current_jobs", []))
                with store.write() as connection:
                    connection.execute(
                        s.telegram_bindings.update()
                        .where(
                            s.telegram_bindings.c.chat_id == chat_id,
                            s.telegram_bindings.c.user_id == owner,
                        )
                        .values(enabled=enabled)
                    )
                response = (
                    "Уведомления включены; текущая выдача принята за базовую."
                    if enabled
                    else "Уведомления выключены."
                )
            elif command == "/settings":
                profile = store.preferences()["search_profile"]["profile"] or {}
                response = "Ваши роли: " + escape(
                    ", ".join(profile.get("role_keywords", []))[:2000]
                )
            elif command == "/sources":
                response = "Ваши источники: " + escape(
                    ", ".join(
                        row["name"]
                        for row in store.preferences()["sources"]["items"]
                        if row["enabled"]
                    )[:2000]
                )
            elif command == "/stats":
                response = (
                    f"В вашей текущей выдаче: {store.list_jobs('active', 1)['total']} вакансий."
                )
            else:
                response = (
                    "/search · /alerts_on · /alerts_off · /settings · /sources · /stats · /whoami"
                )
            await self.send(chat_id, response, owner)
        except StoreError as error:
            await self.send(
                chat_id, "Действие недоступно. Проверьте аккаунт и настройки поиска в кабинете."
            )
            logger.warning("Personal Telegram command rejected: status=%s", error.status)

    async def deliver(self, chat_id, store, only_new=True):
        identifiers = store.get_setting("current_jobs", [])
        with store.read() as connection:
            delivered = set(
                connection.execute(
                    select(s.deliveries.c.job_id).where(
                        s.deliveries.c.user_id == store.user_id, s.deliveries.c.chat_id == chat_id
                    )
                ).scalars()
            )
        count = 0
        for job_id in identifiers:
            if only_new:
                with store.read() as connection:
                    enabled = connection.execute(
                        select(s.telegram_bindings.c.enabled).where(
                            s.telegram_bindings.c.chat_id == chat_id,
                            s.telegram_bindings.c.user_id == store.user_id,
                        )
                    ).scalar_one_or_none()
                if not enabled:
                    return count
            if only_new and job_id in delivered:
                continue
            job = store.get_job(job_id)
            if job["applied_at"] or job["archived_at"]:
                continue
            url = job["url"]
            link = (
                f'<a href="{escape(url, quote=True)}">Открыть вакансию</a>'
                if urlsplit(url).scheme in {"http", "https"}
                else ""
            )
            await self.send(
                chat_id,
                f"<b>{escape(job['title'])}</b>\n{escape(job['company'])}\n"
                f"{escape(job['location'])}\nРейтинг: {job['score']}/100\n{link}",
                store.user_id,
            )
            self.acknowledge(store, chat_id, [job_id])
            count += 1
            if count == 5:
                break
        return count

    async def pending(self):
        if maintenance_active(self.settings.data_dir):
            return
        with self.database.transaction() as connection:
            rows = (
                connection.execute(
                    select(s.runtime_control).where(
                        s.runtime_control.c.key.startswith("telegram-pending:")
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            data = row["payload"]
            if self.owner(data["chat_id"]) != data["owner_id"]:
                continue
            store = PostgresStore(self.database, data["owner_id"])
            task = store.task(data["task_id"])
            if task["status"] in {"queued", "running", "verifying", "rendering"}:
                continue
            if task["status"] == "ready":
                count = await self.deliver(data["chat_id"], store, only_new=False)
                if not count:
                    await self.send(data["chat_id"], "Подходящих вакансий пока нет.", store.user_id)
            else:
                await self.send(
                    data["chat_id"],
                    "Поиск не завершён. Проверьте историю в кабинете.",
                    store.user_id,
                )
            with store.write() as connection:
                connection.execute(
                    s.runtime_control.delete().where(
                        s.runtime_control.c.key == row["key"],
                        s.runtime_control.c.payload == data,
                    )
                )

    async def alerts(self):
        if maintenance_active(self.settings.data_dir):
            return
        with self.database.transaction() as connection:
            rows = (
                connection.execute(
                    select(s.telegram_bindings).where(s.telegram_bindings.c.enabled.is_(True))
                )
                .mappings()
                .all()
            )
        for row in rows:
            if self.owner(row["chat_id"]) != row["user_id"]:
                continue
            try:
                await self.deliver(row["chat_id"], PostgresStore(self.database, row["user_id"]))
            except Exception as error:
                logger.warning("Personal Telegram delivery failed: %s", type(error).__name__)

    async def run(self, stop):
        try:
            with singleton(self.database) as connection:
                self.lock = connection
                while maintenance_active(self.settings.data_dir) and not stop.is_set():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), 1)
                if stop.is_set():
                    return
                await self.api.call("deleteWebhook", drop_pending_updates=False)
                while not stop.is_set():
                    if maintenance_active(self.settings.data_dir):
                        with suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), 1)
                        continue
                    connection.execute(text("SELECT 1"))
                    with self.database.transaction() as read:
                        payload = (
                            read.execute(
                                select(s.runtime_control.c.payload).where(
                                    s.runtime_control.c.key == "telegram-offset"
                                )
                            ).scalar_one_or_none()
                            or {}
                        )
                    updates = await self.api.call(
                        "getUpdates",
                        offset=payload.get("offset", 0),
                        timeout=5,
                        allowed_updates=["message"],
                    )
                    for update in updates:
                        await self.handle(update)
                        with self.database.transaction() as write:
                            statement = insert(s.runtime_control).values(
                                key="telegram-offset",
                                payload={"offset": int(update["update_id"]) + 1},
                            )
                            write.execute(
                                statement.on_conflict_do_update(
                                    index_elements=[s.runtime_control.c.key],
                                    set_={"payload": statement.excluded.payload},
                                )
                            )
                    await self.pending()
                    await self.alerts()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), 1)
        finally:
            self.lock = None
            await self.api.close()


async def async_main():
    from qa_job_bot.pg_database import Database

    database = Database.from_env()
    try:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        if not ready(database):
            raise RuntimeError("Run PostgreSQL migrations before Telegram polling")
        token = private_text(WebSettings.from_env().data_dir / "telegram-bot-token", 256)
        if not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", token):
            raise ValueError("Invalid Telegram token")
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        bot = PersonalTelegram(database, TelegramAPI(token, 15))
        await supervised(database, "telegram", lambda: bot.run(stop), stop)
    except Exception as error:
        logger.error("Personal Telegram stopped: %s", type(error).__name__)
        raise RuntimeError(
            "Personal Telegram stopped; check readiness, configuration and network"
        ) from None
    finally:
        database.close()


def main():
    from qa_job_bot.pg_database import Database

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    binding = commands.add_parser("bind")
    binding.add_argument("--chat-id", type=int, required=True)
    binding.add_argument("--login", required=True)
    alerts = binding.add_mutually_exclusive_group()
    alerts.add_argument("--alerts", action="store_true", default=None)
    alerts.add_argument("--no-alerts", dest="alerts", action="store_false")
    arguments = parser.parse_args()
    if arguments.command == "run":
        asyncio.run(async_main())
    else:
        database = Database.from_env()
        try:
            print(json.dumps(bind(database, arguments.chat_id, arguments.login, arguments.alerts)))
        finally:
            database.close()


if __name__ == "__main__":
    main()
