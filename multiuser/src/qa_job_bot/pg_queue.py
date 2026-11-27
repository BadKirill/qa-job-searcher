import time

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.web_store import ACTIVE_STATUSES

LANES = {
    "search": ("search", "posting_check"),
    "chat": ("chat",),
    "document": ("cv", "cover_letter"),
}


def touch_owner(connection, user_id):
    statement = insert(s.revisions).values(user_id=user_id, revision=1)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id"], set_={"revision": s.revisions.c.revision + 1}
        )
    )


class QueueRepository:
    def __init__(self, database):
        self.database = database

    def expire(self, connection, now, user_id):
        query = select(s.tasks).where(
            s.tasks.c.status.in_(ACTIVE_STATUSES), s.tasks.c.lease_until < now
        )
        if user_id is not None:
            query = query.where(s.tasks.c.user_id == user_id)
        rows = connection.execute(query.with_for_update(skip_locked=True)).mappings().all()
        for row in rows:
            retry = (
                row["kind"] == "search"
                and row["payload"].get("search_inputs")
                and row["attempts"] < 3
            )
            interrupted = (
                "Поиск прервался. Запустите его ещё раз."
                if row["kind"] == "search"
                else "Исполнение прервано. Исход API может быть неизвестен. Повторите задание."
            )
            connection.execute(
                s.tasks.update()
                .where(s.tasks.c.id == row["id"])
                .values(
                    status="queued" if retry else "failed",
                    worker_id=None,
                    lease_until=None,
                    error=None if retry else interrupted,
                )
            )
            touch_owner(connection, row["user_id"])

    def claim(self, worker_id, lease_seconds=900, user_id=None, lane=None):
        from qa_job_bot.pg_store import PostgresStore
        from qa_job_bot.runtime_limits import DEFAULTS, control, save_control

        now = time.time()
        kinds = LANES[lane] if lane else tuple(kind for group in LANES.values() for kind in group)
        with self.database.transaction() as connection:
            state = control(connection)
            self.expire(connection, now, user_id)
            limits = {**DEFAULTS, **state.get("limits", {})}
            active = connection.execute(
                select(s.tasks.c.user_id, s.tasks.c.kind).where(
                    s.tasks.c.status.in_(["running", "verifying", "rendering"]),
                    s.tasks.c.lease_until >= now,
                )
            ).all()
            if lane and sum(row.kind in kinds for row in active) >= limits[lane + "_slots"]:
                return None
            query = (
                select(s.tasks.c.user_id, s.tasks.c.created_at, s.tasks.c.id)
                .join(s.users)
                .where(
                    s.tasks.c.status == "queued",
                    s.tasks.c.kind.in_(kinds),
                    s.users.c.disabled.is_(False),
                )
            )
            if user_id is not None:
                query = query.where(s.tasks.c.user_id == user_id)
            if lane:
                query = query.where(s.users.c.password_hash.is_not(None))
            candidates = connection.execute(
                query.order_by(s.tasks.c.created_at, s.tasks.c.id)
            ).all()
            turns = state.get("turns", {}).get(lane or "legacy", {})
            candidates = sorted(
                candidates, key=lambda row: (turns.get(row.user_id, 0), row.created_at, row.id)
            )
            row = None
            for candidate in candidates:
                maximum = limits["per_user_documents"] if lane == "document" else 1
                if (
                    lane
                    and sum(
                        item.user_id == candidate.user_id and item.kind in kinds for item in active
                    )
                    >= maximum
                ):
                    continue
                row = (
                    connection.execute(
                        select(s.tasks)
                        .where(s.tasks.c.id == candidate.id, s.tasks.c.status == "queued")
                        .with_for_update(skip_locked=True)
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    break
            if row is None:
                return None
            task_id, owner_id = row["id"], row["user_id"]
            connection.execute(
                update(s.tasks)
                .where(s.tasks.c.id == task_id)
                .values(
                    status="running",
                    worker_id=worker_id,
                    lease_until=now + lease_seconds,
                    attempts=s.tasks.c.attempts + 1,
                    error=None,
                )
            )
            sequence = state.get("sequence", 0) + 1
            state["sequence"] = sequence
            state["turns"] = {
                **state.get("turns", {}),
                lane or "legacy": {**turns, owner_id: sequence},
            }
            save_control(connection, state)
            touch_owner(connection, owner_id)
        return PostgresStore(self.database, owner_id).task(task_id, internal=True)

    def heartbeat(self, task_id, worker_id, lease_seconds=900):
        now = time.time()
        with self.database.transaction() as connection:
            enabled = select(s.users.c.id).where(s.users.c.disabled.is_(False))
            changed = connection.execute(
                s.tasks.update()
                .where(
                    s.tasks.c.id == task_id,
                    s.tasks.c.worker_id == worker_id,
                    s.tasks.c.user_id.in_(enabled),
                    s.tasks.c.lease_until >= now,
                    s.tasks.c.status.in_(["running", "verifying", "rendering"]),
                )
                .values(lease_until=now + lease_seconds)
            ).rowcount
            return bool(changed)
