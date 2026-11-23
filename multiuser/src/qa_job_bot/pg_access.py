import time
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_database import REVISION
from qa_job_bot.web_store import StoreError


def upsert(connection, table, values, keys):
    statement = insert(table).values(**values)
    changes = {key: statement.excluded[key] for key in values if key not in keys}
    connection.execute(statement.on_conflict_do_update(index_elements=keys, set_=changes))


def serialize(row):
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


class OwnedRepository:
    def __init__(self, database, user_id: str):
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("An explicit owner is required")
        self.database = database
        self.user_id = user_id

    def initialize(self):
        if self.database.revision() != REVISION:
            raise RuntimeError("Run PostgreSQL migrations before starting the application")
        self.database.user(self.user_id)

    @contextmanager
    def read(self):
        with self.database.transaction() as connection:
            self._account(connection)
            yield connection

    @contextmanager
    def write(self):
        with self.database.transaction() as connection:
            self._account(connection, lock=True)
            yield connection

    def _account(self, connection, lock=False):
        query = select(s.users).where(s.users.c.id == self.user_id)
        if lock:
            query = query.with_for_update()
        row = connection.execute(query).mappings().first()
        if row is None or row["disabled"]:
            raise StoreError("Аккаунт недоступен.", 403)
        return row

    def owned(self, table):
        return table.c.user_id == self.user_id

    def row(self, connection, table, entity_id, message="Объект не найден."):
        row = (
            connection.execute(select(table).where(self.owned(table), table.c.id == entity_id))
            .mappings()
            .first()
        )
        if row is None:
            raise StoreError(message, 404)
        return row

    def touch(self, connection):
        statement = insert(s.revisions).values(user_id=self.user_id, revision=1)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id"], set_={"revision": s.revisions.c.revision + 1}
            )
        )

    def setting(self, connection, key, default=None):
        if key == "password":
            return self._account(connection)["password_hash"] or default
        if key == "revision":
            return (
                connection.execute(
                    select(s.revisions.c.revision).where(self.owned(s.revisions))
                ).scalar_one_or_none()
                or default
            )
        row = connection.execute(
            select(s.settings.c.value).where(self.owned(s.settings), s.settings.c.key == key)
        ).first()
        return row[0] if row else default

    def get_setting(self, key, default=None):
        with self.read() as connection:
            return self.setting(connection, key, default)

    def put_setting(self, connection, key, value):
        if key in {"password", "revision"}:
            raise StoreError("Системное поле недоступно для изменения.", 422)
        upsert(
            connection,
            s.settings,
            {"user_id": self.user_id, "key": key, "value": value},
            ["user_id", "key"],
        )

    def set_setting(self, key, value):
        with self.write() as connection:
            self.put_setting(connection, key, value)
            self.touch(connection)

    def set_initial_password(self, hashed):
        with self.write() as connection:
            if self._account(connection)["password_hash"] is not None:
                raise StoreError("Первоначальная настройка уже завершена.", 409)
            connection.execute(
                update(s.users).where(s.users.c.id == self.user_id).values(password_hash=hashed)
            )

    def record_login_attempt(self, ip, now):
        with self.write() as connection:
            connection.execute(
                delete(s.login_attempts).where(
                    self.owned(s.login_attempts), s.login_attempts.c.created_at < now - 900
                )
            )
            count = connection.execute(
                select(func.count())
                .select_from(s.login_attempts)
                .where(self.owned(s.login_attempts), s.login_attempts.c.ip == ip)
            ).scalar_one()
            if count >= 10:
                raise StoreError("Слишком много попыток. Подождите 15 минут.", 429)
            connection.execute(
                s.login_attempts.insert().values(user_id=self.user_id, ip=ip, created_at=now)
            )

    def clear_login_attempts(self, ip):
        with self.write() as connection:
            connection.execute(
                delete(s.login_attempts).where(
                    self.owned(s.login_attempts), s.login_attempts.c.ip == ip
                )
            )

    def save_session(self, hashed, csrf, now, expires_at):
        with self.write() as connection:
            connection.execute(
                delete(s.sessions).where(self.owned(s.sessions), s.sessions.c.expires_at < now)
            )
            connection.execute(
                s.sessions.insert().values(
                    token_hash=hashed, user_id=self.user_id, csrf=csrf, expires_at=expires_at
                )
            )

    def find_session(self, hashed, now):
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(s.sessions)
                    .join(s.users)
                    .where(
                        self.owned(s.sessions),
                        s.sessions.c.token_hash == hashed,
                        s.sessions.c.expires_at > now,
                        s.users.c.disabled.is_(False),
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def delete_session(self, hashed):
        with self.database.transaction() as connection:
            connection.execute(
                delete(s.sessions).where(self.owned(s.sessions), s.sessions.c.token_hash == hashed)
            )

    def live_lease(self, task_id, worker_id):
        return (
            self.owned(s.tasks),
            s.tasks.c.id == task_id,
            s.tasks.c.worker_id == worker_id,
            s.tasks.c.status.in_(["running", "verifying", "rendering"]),
            s.tasks.c.lease_until >= time.time(),
        )
