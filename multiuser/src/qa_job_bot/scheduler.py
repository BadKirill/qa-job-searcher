import asyncio
import hashlib
import signal
import time
from contextlib import suppress

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.maintenance import active as maintenance_active
from qa_job_bot.operations import supervised
from qa_job_bot.pg_database import REVISION, Database
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.runtime_limits import control
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError


def schedule_in(connection, user_id, now):
    offset = int(hashlib.sha256(user_id.encode()).hexdigest()[:8], 16) % 10800 + 1
    statement = insert(s.search_schedules).values(user_id=user_id, next_run_at=now + offset)
    connection.execute(statement.on_conflict_do_nothing())
    return dict(
        connection.execute(
            select(s.search_schedules).where(s.search_schedules.c.user_id == user_id)
        )
        .mappings()
        .one()
    )


class Scheduler:
    def __init__(self, database, settings=None):
        self.database = database
        self.settings = settings or WebSettings.from_env()

    def tick(self):
        if maintenance_active(self.settings.data_dir):
            return []
        now, created = time.time(), []
        with self.database.transaction() as connection:
            control(connection)
            users = (
                connection.execute(
                    select(s.users.c.id).where(
                        s.users.c.disabled.is_(False), s.users.c.password_hash.is_not(None)
                    )
                )
                .scalars()
                .all()
            )
            for user_id in users:
                row = schedule_in(connection, user_id, now)
                if not row["enabled"] or row["next_run_at"] > now:
                    continue
                store = PostgresStore(self.database, user_id)
                try:
                    with connection.begin_nested():
                        store._account(connection, lock=True)
                        task = store.enqueue_search_in(
                            connection, f"scheduled:{row['next_run_at']}"
                        )
                        created.append(task["id"])
                except StoreError as error:
                    if error.status not in {403, 409}:
                        raise
                interval = row["interval_seconds"]
                next_run = (
                    row["next_run_at"]
                    + (int((now - row["next_run_at"]) // interval) + 1) * interval
                )
                connection.execute(
                    s.search_schedules.update()
                    .where(s.search_schedules.c.user_id == user_id)
                    .values(next_run_at=next_run)
                )
        return created

    async def run(self, stop):
        while not stop.is_set():
            self.tick()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)


async def async_main():
    database = Database.from_env()
    if database.revision() != REVISION:
        database.close()
        raise RuntimeError("Run PostgreSQL migrations before starting scheduler")
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await supervised(database, "scheduler", lambda: Scheduler(database).run(stop), stop)
    finally:
        database.close()


def main():
    with suppress(KeyboardInterrupt):
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
