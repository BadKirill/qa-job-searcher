import time

from pydantic import Field
from sqlalchemy import select

from qa_job_bot import pg_schema as s
from qa_job_bot.account_routes import AccountInput, User
from qa_job_bot.operations import diagnostics
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.pilot import Pilot
from qa_job_bot.runtime_limits import DEFAULTS, RuntimeLimits
from qa_job_bot.scheduler import schedule_in


class LimitsInput(AccountInput):
    search_slots: int = Field(default=2, ge=1, le=16)
    chat_slots: int = Field(default=2, ge=1, le=16)
    document_slots: int = Field(default=2, ge=1, le=16)
    per_user_documents: int = Field(default=1, ge=1, le=4)
    api_slots: int = Field(default=2, ge=1, le=16)
    daily_calls: int = Field(default=600, ge=0, le=1_000_000)
    user_daily_calls: int = Field(default=60, ge=0, le=1_000_000)


class PersonalLimit(AccountInput):
    daily_calls: int = Field(ge=0, le=1_000_000)


class ScheduleInput(AccountInput):
    enabled: bool
    interval_seconds: int = Field(default=10800, ge=900, le=86400)


def register_runtime_routes(app, accounts):
    database = accounts.database
    runtime = RuntimeLimits(database)

    @app.get("/api/admin/pilot")
    def pilot_status(user: User):
        with database.transaction() as connection:
            accounts.actor(connection, user, admin=True)
        return Pilot(database).status()

    @app.get("/api/admin/diagnostics")
    def operational_status(user: User):
        with database.transaction() as connection:
            accounts.actor(connection, user, admin=True)
        return diagnostics(database)

    @app.get("/api/usage")
    def usage(user: User):
        return runtime.summary(user.id)

    @app.get("/api/schedule")
    def schedule(user: User):
        with PostgresStore(database, user.id).write() as connection:
            row = schedule_in(connection, user.id, time.time())
        return {key: row[key] for key in ("enabled", "interval_seconds", "next_run_at")}

    @app.put("/api/schedule")
    def update_schedule(data: ScheduleInput, user: User):
        store = PostgresStore(database, user.id)
        with store.write() as connection:
            schedule_in(connection, user.id, time.time())
            connection.execute(
                s.search_schedules.update()
                .where(s.search_schedules.c.user_id == user.id)
                .values(**data.model_dump(), next_run_at=time.time() + data.interval_seconds)
            )
            store.touch(connection)
        return {"saved": True}

    @app.get("/api/admin/runtime")
    def configuration(user: User):
        with database.transaction() as connection:
            accounts.actor(connection, user, admin=True)
            state = (
                connection.execute(
                    select(s.runtime_control.c.payload).where(s.runtime_control.c.key == "global")
                ).scalar_one_or_none()
                or {}
            )
            users = (
                connection.execute(select(s.users.c.id, s.users.c.login).order_by(s.users.c.login))
                .mappings()
                .all()
            )
        return {
            "limits": {**DEFAULTS, **state.get("limits", {})},
            "users": [
                {"id": row["id"], "login": row["login"], **runtime.summary(row["id"])}
                for row in users
            ],
        }

    @app.put("/api/admin/runtime")
    def update_limits(data: LimitsInput, user: User):
        return runtime.configure(user, limits=data.model_dump())

    @app.put("/api/admin/users/{identifier}/limits")
    def update_personal_limit(identifier: str, data: PersonalLimit, user: User):
        runtime.configure(user, user_id=identifier, daily_calls=data.daily_calls)
        return {"saved": True}
