import asyncio
import os
import signal
from contextlib import suppress

from qa_job_bot.agent_gateway import AgentGateway
from qa_job_bot.maintenance import active as maintenance_active
from qa_job_bot.metered_gateway import MeteredGateway
from qa_job_bot.operations import supervised
from qa_job_bot.pg_database import REVISION, Database
from qa_job_bot.pg_queue import LANES, QueueRepository
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.runtime_limits import RuntimeLimits
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError
from qa_job_bot.worker import Worker


class RuntimeWorker:
    def __init__(
        self,
        database,
        settings,
        gateway=None,
        search_pipeline=None,
        heartbeat_seconds=15,
        lease_seconds=90,
    ):
        self.database, self.settings = database, settings
        self.gateway = MeteredGateway(
            gateway or AgentGateway(data_dir=settings.data_dir), RuntimeLimits(database)
        )
        self.pipeline = search_pipeline
        self.queue = QueueRepository(database)
        self.heartbeat_seconds, self.lease_seconds = heartbeat_seconds, lease_seconds

    async def once(self, lane):
        if maintenance_active(self.settings.data_dir):
            return False
        from uuid import uuid4

        execution_id = str(uuid4())
        RuntimeLimits(self.database).reap()
        try:
            task = self.queue.claim(execution_id, self.lease_seconds, lane=lane)
        except StoreError as error:
            if error.status != 403:
                raise
            return False
        if task is None:
            return False
        worker = Worker(
            PostgresStore(self.database, task["user_id"]),
            self.settings,
            self.gateway,
            search_pipeline=self.pipeline,
        )
        worker.id = execution_id
        execution = asyncio.create_task(worker.execute(task))
        try:
            while not execution.done():
                await asyncio.wait({execution}, timeout=self.heartbeat_seconds)
                if execution.done():
                    break
                if not self.queue.heartbeat(task["id"], execution_id, self.lease_seconds):
                    execution.cancel()
                    break
            with suppress(asyncio.CancelledError):
                await execution
        finally:
            if not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
        return True

    async def serve(self, lane, stop):
        while not stop.is_set():
            try:
                worked = await self.once(lane)
            except StoreError as error:
                if error.status != 403:
                    raise
                worked = False
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.25)

    async def run(self, stop, lanes=None):
        lanes = lanes or {"search": 2, "chat": 2, "document": 2}
        async with asyncio.TaskGroup() as group:
            for lane, count in lanes.items():
                for _ in range(count):
                    group.create_task(self.serve(lane, stop))


async def async_main():
    database = Database.from_env()
    settings = WebSettings.from_env()
    if database.revision() != REVISION:
        database.close()
        raise RuntimeError("Run PostgreSQL migrations before starting workers")
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    selected = os.getenv("WORKER_LANE", "all")
    try:
        if selected not in {*LANES, "all"}:
            raise ValueError("WORKER_LANE must be all, search, chat or document")
        await supervised(
            database,
            selected,
            lambda: RuntimeWorker(database, settings).run(
                stop, None if selected == "all" else {selected: 2}
            ),
            stop,
        )
    finally:
        database.close()


def main():
    with suppress(KeyboardInterrupt):
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
