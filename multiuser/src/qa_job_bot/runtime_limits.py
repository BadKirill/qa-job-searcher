import re
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Float, or_, select
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_queue import touch_owner
from qa_job_bot.web_store import StoreError

DEFAULTS = {
    "search_slots": 2,
    "chat_slots": 2,
    "document_slots": 2,
    "per_user_documents": 1,
    "api_slots": 2,
    "daily_calls": 600,
    "user_daily_calls": 60,
}


class QuotaExceeded(RuntimeError):
    pass


def control(connection):
    statement = insert(s.runtime_control).values(key="global", payload={})
    connection.execute(statement.on_conflict_do_nothing())
    return dict(
        connection.execute(
            select(s.runtime_control.c.payload)
            .where(s.runtime_control.c.key == "global")
            .with_for_update()
        ).scalar_one()
    )


def save_control(connection, data):
    connection.execute(
        s.runtime_control.update().where(s.runtime_control.c.key == "global").values(payload=data)
    )


def active_task(connection, context):
    now = time.time()
    row = (
        connection.execute(
            select(s.tasks)
            .join(s.users)
            .where(
                s.tasks.c.id == context["task_id"],
                s.tasks.c.user_id == context["owner_id"],
                s.tasks.c.worker_id == context["execution_id"],
                s.tasks.c.status.in_(["running", "verifying", "rendering"]),
                s.tasks.c.lease_until >= now,
                s.users.c.disabled.is_(False),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise QuotaExceeded("Задание отменено, аккаунт отключён или истёк срок исполнения.")
    return row


class RuntimeLimits:
    def __init__(self, database):
        self.database = database

    def reserve(self, context, role):
        now = time.time()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self.database.transaction() as connection:
            state = control(connection)
            active_task(connection, context)
            limits = {**DEFAULTS, **state.get("limits", {})}
            rows = (
                connection.execute(
                    select(s.usage_reservations).where(
                        or_(
                            s.usage_reservations.c.payload["day"].astext == day,
                            s.usage_reservations.c.payload["expires_at"].astext.cast(Float) > now,
                        )
                    )
                )
                .mappings()
                .all()
            )
            entries = [
                row
                for row in rows
                if row["payload"].get("day") == day and row["payload"].get("state") != "released"
            ]
            personal = state.get("users", {}).get(context["owner_id"], limits["user_daily_calls"])
            if len(entries) >= limits["daily_calls"]:
                raise QuotaExceeded(
                    "Общий суточный лимит OpenAI исчерпан. Обратитесь к администратору."
                )
            if sum(row["user_id"] == context["owner_id"] for row in entries) >= personal:
                raise QuotaExceeded(
                    "Ваш суточный лимит OpenAI исчерпан. Обратитесь к администратору."
                )
            occupied = sum(
                (
                    row["payload"].get("state") in {"reserved", "sent"}
                    or (
                        row["payload"].get("state") == "unknown"
                        and not row["payload"].get("terminal")
                    )
                )
                and row["payload"].get("expires_at", 0) > now
                for row in rows
            )
            personal_occupied = any(
                row["user_id"] == context["owner_id"]
                and row["payload"].get("expires_at", 0) > now
                and (
                    row["payload"].get("state") in {"reserved", "sent"}
                    or (
                        row["payload"].get("state") == "unknown"
                        and not row["payload"].get("terminal")
                    )
                )
                for row in rows
            )
            if (
                occupied >= limits["api_slots"]
                or personal_occupied
                or state.get("cooldown_until", 0) > now
            ):
                return None
            identifier = str(uuid4())
            payload = {
                "day": day,
                "role": role,
                "state": "reserved",
                "usage": None,
                "worker_id": context["execution_id"],
                "expires_at": now + 300,
            }
            connection.execute(
                s.usage_reservations.insert().values(
                    id=identifier,
                    user_id=context["owner_id"],
                    task_id=context["task_id"],
                    payload=payload,
                )
            )
            touch_owner(connection, context["owner_id"])
            return identifier

    def progress(self, identifier, event, data):
        from qa_job_bot.openai_transport import token_usage

        with self.database.transaction() as connection:
            state = control(connection)
            row = (
                connection.execute(
                    select(s.usage_reservations)
                    .where(s.usage_reservations.c.id == identifier)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            payload = dict(row["payload"])
            if payload["state"] not in {"reserved", "sent"}:
                if event == "sent":
                    raise QuotaExceeded("Резерв API больше не действует.")
                return
            if event == "sent":
                active_task(
                    connection,
                    {
                        "owner_id": row["user_id"],
                        "task_id": row["task_id"],
                        "execution_id": payload["worker_id"],
                    },
                )
                payload["state"] = "sent"
            elif event == "rate_limit":
                state["cooldown_until"] = max(
                    state.get("cooldown_until", 0), time.time() + data["retry_after"]
                )
                save_control(connection, state)
            else:
                failure = data.get("failure_type")
                if (
                    event == "failure"
                    and isinstance(failure, str)
                    and re.fullmatch(r"[A-Za-z_]{1,80}", failure)
                ):
                    payload["failure_type"] = failure
                if event in {"usage", "rejected"}:
                    payload["terminal"] = True
                payload.update(
                    {
                        key: value
                        for key, value in data.items()
                        if key in {"session_id", "turn_id", "usage"}
                    }
                )
                if "usage" in data:
                    payload["usage"] = token_usage(data["usage"])
            connection.execute(
                s.usage_reservations.update()
                .where(s.usage_reservations.c.id == identifier)
                .values(payload=payload)
            )

    def finish(self, identifier, succeeded):
        with self.database.transaction() as connection:
            control(connection)
            row = (
                connection.execute(
                    select(s.usage_reservations)
                    .where(s.usage_reservations.c.id == identifier)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            payload = dict(row["payload"])
            if payload["state"] not in {"reserved", "sent"}:
                return
            payload["state"] = (
                "completed"
                if succeeded
                else (
                    "failed"
                    if payload.get("terminal")
                    else "unknown"
                    if payload["state"] == "sent"
                    else "released"
                )
            )
            payload["finished_at"] = time.time()
            connection.execute(
                s.usage_reservations.update()
                .where(s.usage_reservations.c.id == identifier)
                .values(payload=payload)
            )
            connection.execute(
                s.usage_events.insert().values(
                    id=str(uuid4()),
                    user_id=row["user_id"],
                    task_id=row["task_id"],
                    role=payload["role"],
                    external_id=identifier,
                    payload={**payload, "reservation_id": identifier},
                )
            )
            touch_owner(connection, row["user_id"])

    def reap(self):
        with self.database.transaction() as connection:
            rows = (
                connection.execute(
                    select(s.usage_reservations).where(
                        s.usage_reservations.c.payload["state"].astext.in_(["reserved", "sent"]),
                        s.usage_reservations.c.payload["expires_at"].astext.cast(Float)
                        < time.time(),
                    )
                )
                .mappings()
                .all()
            )
            expired = [
                row["id"]
                for row in rows
                if row["payload"].get("state") in {"reserved", "sent"}
                and row["payload"].get("expires_at", 0) < time.time()
            ]
        for identifier in expired:
            self.finish(identifier, False)
        return len(expired)

    def summary(self, user_id):
        now = time.time()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self.database.transaction() as connection:
            state = (
                connection.execute(
                    select(s.runtime_control.c.payload).where(s.runtime_control.c.key == "global")
                ).scalar_one_or_none()
                or {}
            )
            limits = {**DEFAULTS, **state.get("limits", {})}
            rows = (
                connection.execute(
                    select(s.usage_reservations.c.payload).where(
                        s.usage_reservations.c.user_id == user_id,
                        s.usage_reservations.c.payload["day"].astext == day,
                    )
                )
                .scalars()
                .all()
            )
            today = [
                row for row in rows if row.get("day") == day and row.get("state") != "released"
            ]
            return {
                "day": day,
                "daily_calls": state.get("users", {}).get(user_id, limits["user_daily_calls"]),
                "used_calls": len(today),
                "unknown_calls": sum(row.get("usage") is None for row in today),
                "tokens": sum(
                    row["usage"]["total_tokens"] for row in today if row.get("usage") is not None
                ),
                "cooldown_until": state.get("cooldown_until", 0),
            }

    def configure(self, actor, limits=None, user_id=None, daily_calls=None):
        with self.database.transaction() as connection:
            state = control(connection)
            row = (
                connection.execute(select(s.users).where(s.users.c.id == actor.id))
                .mappings()
                .first()
            )
            if row is None or row["disabled"] or row["role"] != "admin":
                raise StoreError("Нужны права администратора.", 403)
            if user_id is not None:
                if (
                    connection.execute(select(s.users.c.id).where(s.users.c.id == user_id)).first()
                    is None
                ):
                    raise StoreError("Аккаунт не найден.", 404)
                state["users"] = {**state.get("users", {}), user_id: daily_calls}
            else:
                state["limits"] = {**state.get("limits", {}), **limits}
            save_control(connection, state)
            return {**DEFAULTS, **state.get("limits", {})}
