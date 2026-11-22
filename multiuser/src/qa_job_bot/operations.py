import asyncio
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import Float, func, select, text
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_database import REVISION, Database

ACTIVE = ("running", "verifying", "rendering")
SERVICES = ("scheduler", "search", "chat", "document", "all", "telegram")


def ready(database):
    try:
        return database.revision() == REVISION
    except Exception:
        return False


def diagnostics(database):
    now = time.time()
    with database.transaction() as connection:
        queue = [
            {"kind": kind, "status": status, "count": count}
            for kind, status, count in connection.execute(
                select(s.tasks.c.kind, s.tasks.c.status, func.count())
                .group_by(s.tasks.c.kind, s.tasks.c.status)
                .order_by(s.tasks.c.kind, s.tasks.c.status)
            )
        ]
        expired = connection.execute(
            select(func.count())
            .select_from(s.tasks)
            .where(s.tasks.c.status.in_(ACTIVE), s.tasks.c.lease_until < now)
        ).scalar_one()
        oldest = connection.execute(
            select(func.min(s.tasks.c.created_at)).where(s.tasks.c.status == "queued")
        ).scalar_one()
        usage = dict(
            connection.execute(
                select(
                    s.usage_reservations.c.payload["state"].astext.label("state"), func.count()
                ).group_by("state")
            ).all()
        )
        unknown = connection.execute(
            select(func.count())
            .select_from(s.usage_reservations)
            .where(
                s.usage_reservations.c.payload["state"].astext != "released",
                s.usage_reservations.c.payload["usage"].astext.is_(None),
            )
        ).scalar_one()
        state = (
            connection.execute(
                select(s.runtime_control.c.payload).where(s.runtime_control.c.key == "global")
            ).scalar_one_or_none()
            or {}
        )
        services = dict(
            connection.execute(
                select(s.runtime_control.c.payload["service"].astext.label("service"), func.count())
                .where(
                    s.runtime_control.c.key.startswith("service:"),
                    s.runtime_control.c.payload["at"].astext.cast(Float) > now - 60,
                )
                .group_by("service")
            ).all()
        )
        source_errors = connection.execute(
            text(
                "SELECT count(*) FROM search_runs, "
                "jsonb_array_elements(COALESCE(payload->'sources', '[]'::jsonb)) entry "
                "WHERE COALESCE(entry->>'error', '') <> ''"
            )
        ).scalar_one()
    return {
        "queue": queue,
        "expired_leases": expired,
        "oldest_wait_seconds": max(0, int(now - oldest.timestamp())) if oldest else 0,
        "usage_states": usage,
        "unknown_usage": unknown,
        "cooldown_seconds": max(0, int(state.get("cooldown_until", 0) - now)),
        "source_errors": source_errors,
        "services": {name: services.get(name, 0) for name in SERVICES},
    }


async def supervised(database, service, operation, stop, path=None):
    identifier = "service:" + str(uuid4())
    marker = Path(path or os.environ.get("SERVICE_HEALTH_FILE", "/tmp/job-searcher-health"))
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"id": identifier, "service": service}, output)

    async def pulse():
        while not stop.is_set():
            with database.transaction() as connection:
                statement = insert(s.runtime_control).values(
                    key=identifier, payload={"service": service, "at": time.time()}
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[s.runtime_control.c.key],
                        set_={"payload": statement.excluded.payload},
                    )
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 10)

    async def execute():
        try:
            await operation()
        finally:
            stop.set()

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(pulse())
            group.create_task(execute())
    finally:
        marker.unlink(missing_ok=True)
        with database.transaction() as connection:
            connection.execute(
                s.runtime_control.delete().where(s.runtime_control.c.key == identifier)
            )


def check_service(database, path):
    try:
        marker = Path(path)
        if marker.is_symlink() or marker.stat().st_mode & 0o077:
            return False
        identity = json.loads(marker.read_text())
        with database.transaction() as connection:
            payload = connection.execute(
                select(s.runtime_control.c.payload).where(s.runtime_control.c.key == identity["id"])
            ).scalar_one_or_none()
        return bool(
            payload
            and payload["service"] == identity["service"]
            and payload["at"] > time.time() - 60
        )
    except Exception:
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=("api", "service"))
    arguments = parser.parse_args()
    if arguments.target == "api":
        try:
            address = os.environ.get("HEALTH_URL", "http://127.0.0.1:8000/health/ready")
            origin = os.getenv("WEB_ORIGINS", address).split(",")[0].strip()
            with urlopen(
                Request(address, headers={"Host": urlsplit(origin).netloc}), timeout=5
            ) as response:
                healthy = response.status == 200
        except Exception:
            healthy = False
    else:
        database = Database.from_env()
        try:
            healthy = ready(database) and check_service(
                database, os.environ.get("SERVICE_HEALTH_FILE", "/tmp/job-searcher-health")
            )
        finally:
            database.close()
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
