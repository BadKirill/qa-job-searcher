import hmac
import os
import re
import secrets
import time
import unicodedata
from dataclasses import asdict, dataclass
from uuid import uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_database import REVISION
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web_security import password_hash, token_hash
from qa_job_bot.web_store import StoreError

SESSION_TTL = 86400
WINDOW = 900
DUMMY_HASH = password_hash("synthetic-invalid-password", "0" * 32)
AUTH_ERROR = "Неверный логин или пароль."
TOKEN_ERROR = "Ссылка недействительна или срок её действия истёк."


@dataclass(frozen=True)
class CurrentUser:
    id: str
    login: str
    display_name: str
    role: str

    @classmethod
    def from_row(cls, row):
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


def normalize_login(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def valid_identity(login, display_name):
    login, display_name = normalize_login(login), display_name.strip()
    if not re.fullmatch(r"[\w.@+-]{3,100}", login) or not 1 <= len(display_name) <= 120:
        raise StoreError("Логин: 3–100 букв, цифр или . @ + - _. Укажите имя до 120 символов.", 422)
    return login, display_name


def check_password(password, saved):
    candidate = (
        saved
        if isinstance(saved, str) and re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{128}", saved)
        else DUMMY_HASH
    )
    computed = password_hash(password, candidate.split(":")[0])
    return hmac.compare_digest(computed, candidate) and candidate != DUMMY_HASH


class Accounts:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        if database.revision() != REVISION:
            raise RuntimeError("Run PostgreSQL migrations before starting the application")
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.data_dir.chmod(0o700)
        self.setup_file = settings.data_dir / "setup-token"
        if self.setup_required() and not self.setup_file.exists():
            try:
                descriptor = os.open(self.setup_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32))

    def bootstrap_user(self, connection):
        rows = connection.execute(select(s.users).order_by(s.users.c.id)).mappings().all()
        if (
            len(rows) == 1
            and rows[0]["role"] == "admin"
            and not rows[0]["disabled"]
            and rows[0]["password_hash"] is None
        ):
            return rows[0]
        return None

    def setup_required(self):
        with self.database.transaction() as connection:
            return self.bootstrap_user(connection) is not None

    def rate_limit(self, operation, identity, ip):
        now = time.time()
        key, address = token_hash(operation + ":" + identity), token_hash(ip)
        with self.database.transaction() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(1853392744)"))
            connection.execute(
                delete(s.auth_attempts).where(s.auth_attempts.c.created_at <= now - WINDOW)
            )
            account_count = connection.execute(
                select(func.count())
                .select_from(s.auth_attempts)
                .where(s.auth_attempts.c.key_hash == key)
            ).scalar_one()
            ip_count = connection.execute(
                select(func.count())
                .select_from(s.auth_attempts)
                .where(s.auth_attempts.c.ip_hash == address)
            ).scalar_one()
            if account_count >= 10 or ip_count >= 60:
                raise StoreError("Слишком много попыток. Повторите через 15 минут.", 429)
            connection.execute(
                s.auth_attempts.insert().values(key_hash=key, ip_hash=address, created_at=now)
            )
        return key

    def issue_session(self, connection, user):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        connection.execute(
            s.sessions.insert().values(
                token_hash=token_hash(token),
                user_id=user["id"],
                csrf=csrf,
                expires_at=time.time() + SESSION_TTL,
            )
        )
        return {"token": token, "csrf_token": csrf, "user": asdict(CurrentUser.from_row(user))}

    def setup(self, token, password, ip):
        self.rate_limit("setup", "bootstrap", ip)
        try:
            expected = self.setup_file.read_text().strip()
        except FileNotFoundError:
            expected = ""
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise StoreError("Неверный код первоначальной настройки.", 403)
        hashed = password_hash(password)
        with self.database.transaction() as connection:
            connection.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
            user = self.bootstrap_user(connection)
            if user is None:
                raise StoreError("Первоначальная настройка уже завершена.", 409)
            connection.execute(
                update(s.users).where(s.users.c.id == user["id"]).values(password_hash=hashed)
            )
            result = self.issue_session(connection, user)
        self.setup_file.unlink(missing_ok=True)
        return result

    def login(self, login, password, ip):
        login = normalize_login(login)
        key = self.rate_limit("login", login, ip)
        with self.database.transaction() as connection:
            user = (
                connection.execute(select(s.users).where(s.users.c.login == login))
                .mappings()
                .first()
            )
        saved = user["password_hash"] if user else None
        valid = check_password(password, saved)
        if not valid or user is None or user["disabled"]:
            raise StoreError(AUTH_ERROR, 401)
        with self.database.transaction() as connection:
            current = (
                connection.execute(
                    select(s.users).where(s.users.c.id == user["id"]).with_for_update()
                )
                .mappings()
                .one()
            )
            if current["disabled"] or current["password_hash"] != saved:
                raise StoreError(AUTH_ERROR, 401)
            result = self.issue_session(connection, current)
            connection.execute(delete(s.auth_attempts).where(s.auth_attempts.c.key_hash == key))
        return result

    def session(self, token):
        if not token:
            return None
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(s.users, s.sessions.c.csrf, s.sessions.c.expires_at)
                    .join(s.sessions)
                    .where(
                        s.sessions.c.token_hash == token_hash(token),
                        s.sessions.c.expires_at > time.time(),
                        s.users.c.disabled.is_(False),
                        s.users.c.password_hash.is_not(None),
                    )
                )
                .mappings()
                .first()
            )
            return {"user": CurrentUser.from_row(row), "csrf": row["csrf"]} if row else None

    def authorize(self, token, csrf=None):
        session = self.session(token)
        if session is None:
            raise StoreError("Войдите в приложение.", 401)
        if csrf is not None and not hmac.compare_digest(csrf.encode(), session["csrf"].encode()):
            raise StoreError("Проверка безопасности не пройдена. Обновите страницу.", 403)
        return session

    def logout(self, token):
        with self.database.transaction() as connection:
            connection.execute(
                delete(s.sessions).where(s.sessions.c.token_hash == token_hash(token))
            )

    def actor(self, connection, user, admin=False):
        row = (
            connection.execute(select(s.users).where(s.users.c.id == user.id).with_for_update())
            .mappings()
            .first()
        )
        if row is None or row["disabled"] or (admin and row["role"] != "admin"):
            raise StoreError("Недостаточно прав.", 403)
        return row

    def revoke_sessions(self, user):
        with self.database.transaction() as connection:
            self.actor(connection, user)
            connection.execute(delete(s.sessions).where(s.sessions.c.user_id == user.id))

    def update_profile(self, user, display_name):
        name = unicodedata.normalize("NFKC", display_name).strip()
        if not 1 <= len(name) <= 120 or any(
            unicodedata.category(char).startswith("C") for char in name
        ):
            raise StoreError("Укажите имя от 1 до 120 символов без управляющих знаков.", 422)
        with self.database.transaction() as connection:
            row = self.actor(connection, user)
            connection.execute(
                update(s.users).where(s.users.c.id == user.id).values(display_name=name)
            )
            PostgresStore(self.database, user.id).touch(connection)
            return asdict(CurrentUser.from_row({**row, "display_name": name}))

    def change_password(self, user, old_password, new_password):
        hashed = password_hash(new_password)
        with self.database.transaction() as connection:
            row = self.actor(connection, user)
            if not check_password(old_password, row["password_hash"]):
                raise StoreError("Текущий пароль неверен.", 403)
            connection.execute(
                update(s.users).where(s.users.c.id == user.id).values(password_hash=hashed)
            )
            connection.execute(delete(s.sessions).where(s.sessions.c.user_id == user.id))
            connection.execute(
                update(s.password_resets)
                .where(
                    s.password_resets.c.user_id == user.id, s.password_resets.c.used_at.is_(None)
                )
                .values(revoked_at=time.time())
            )
            return self.issue_session(connection, row)

    def list_users(self, user):
        with self.database.transaction() as connection:
            self.actor(connection, user, admin=True)
            return [
                {
                    **asdict(CurrentUser.from_row(row)),
                    "disabled": row["disabled"],
                    "pending": row["password_hash"] is None,
                }
                for row in connection.execute(
                    select(s.users).order_by(s.users.c.created_at, s.users.c.id)
                ).mappings()
            ]

    def mint_token(self, connection, table, user_id, ttl, created_by):
        now, raw = time.time(), secrets.token_urlsafe(32)
        connection.execute(
            update(table)
            .where(table.c.user_id == user_id, table.c.used_at.is_(None))
            .values(revoked_at=now)
        )
        identifier = str(uuid4())
        connection.execute(
            table.insert().values(
                id=identifier,
                user_id=user_id,
                token_hash=token_hash(raw),
                expires_at=now + ttl,
                created_by_id=created_by,
            )
        )
        return {"id": identifier, "token": raw, "expires_at": now + ttl, "user_id": user_id}

    def invite(self, actor, login, display_name):
        from qa_job_bot.pilot import guard_admission, lock

        login, display_name = valid_identity(login, display_name)
        try:
            with self.database.transaction() as connection:
                lock(connection)
                self.actor(connection, actor, admin=True)
                guard_admission(connection)
                user_id = str(uuid4())
                connection.execute(
                    s.users.insert().values(id=user_id, login=login, display_name=display_name)
                )
                return self.mint_token(connection, s.invitations, user_id, 172800, actor.id)
        except IntegrityError:
            raise StoreError("Этот логин уже занят.", 409) from None

    def issue_access(self, actor, user_id, kind):
        table = s.invitations if kind == "invite" else s.password_resets
        with self.database.transaction() as connection:
            self.actor(connection, actor, admin=True)
            target = (
                connection.execute(select(s.users).where(s.users.c.id == user_id).with_for_update())
                .mappings()
                .first()
            )
            if target is None:
                raise StoreError("Аккаунт не найден.", 404)
            if target["disabled"] or (kind == "invite") != (target["password_hash"] is None):
                raise StoreError("Аккаунт не подходит для этой операции.", 409)
            return self.mint_token(
                connection, table, user_id, 172800 if kind == "invite" else 3600, actor.id
            )

    def invitations(self, actor):
        with self.database.transaction() as connection:
            self.actor(connection, actor, admin=True)
            return [
                dict(row)
                for row in connection.execute(
                    select(
                        s.invitations.c.id,
                        s.invitations.c.user_id,
                        s.invitations.c.expires_at,
                        s.invitations.c.used_at,
                        s.invitations.c.revoked_at,
                    ).order_by(s.invitations.c.created_at)
                ).mappings()
            ]

    def revoke_invitation(self, actor, identifier):
        return self.revoke_token(actor, identifier, "invite")

    def revoke_token(self, actor, identifier, kind):
        table = s.invitations if kind == "invite" else s.password_resets
        with self.database.transaction() as connection:
            self.actor(connection, actor, admin=True)
            row = connection.execute(
                select(table.c.user_id).where(table.c.id == identifier)
            ).first()
            if row is None:
                raise StoreError("Приглашение не найдено.", 404)
            connection.execute(select(s.users).where(s.users.c.id == row.user_id).with_for_update())
            connection.execute(
                update(table)
                .where(table.c.id == identifier, table.c.used_at.is_(None))
                .values(revoked_at=time.time())
            )

    def set_disabled(self, actor, user_id, disabled):
        from qa_job_bot.pilot import guard_admission, lock

        with self.database.transaction() as connection:
            lock(connection)
            self.actor(connection, actor, admin=True)
            if user_id == actor.id:
                raise StoreError("Нельзя отключить собственный аккаунт.", 409)
            target = connection.execute(
                select(s.users).where(s.users.c.id == user_id).with_for_update()
            ).first()
            if target is None:
                raise StoreError("Аккаунт не найден.", 404)
            if not disabled and target.disabled:
                guard_admission(connection)
            connection.execute(
                update(s.users).where(s.users.c.id == user_id).values(disabled=disabled)
            )
            connection.execute(delete(s.sessions).where(s.sessions.c.user_id == user_id))
            if disabled:
                connection.execute(
                    s.tasks.update()
                    .where(
                        s.tasks.c.user_id == user_id,
                        s.tasks.c.status.in_(["queued", "running", "verifying", "rendering"]),
                    )
                    .values(status="cancelled", error="Аккаунт отключён.")
                )
            for table in (s.invitations, s.password_resets):
                connection.execute(
                    update(table)
                    .where(table.c.user_id == user_id, table.c.used_at.is_(None))
                    .values(revoked_at=time.time())
                )

    def redeem(self, kind, token, password, ip):
        self.rate_limit("redeem", token_hash(token), ip)
        hashed = password_hash(password)
        table = s.invitations if kind == "invite" else s.password_resets
        with self.database.transaction() as connection:
            initial = connection.execute(
                select(table.c.user_id).where(table.c.token_hash == token_hash(token))
            ).first()
            if initial is None:
                raise StoreError(TOKEN_ERROR, 403)
            user = (
                connection.execute(
                    select(s.users).where(s.users.c.id == initial.user_id).with_for_update()
                )
                .mappings()
                .one()
            )
            row = (
                connection.execute(
                    select(table).where(table.c.token_hash == token_hash(token)).with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                user["disabled"]
                or row["used_at"] is not None
                or row["revoked_at"] is not None
                or row["expires_at"] <= time.time()
                or (kind == "invite") != (user["password_hash"] is None)
            ):
                raise StoreError(TOKEN_ERROR, 403)
            connection.execute(
                update(table).where(table.c.id == row["id"]).values(used_at=time.time())
            )
            connection.execute(
                update(s.users).where(s.users.c.id == user["id"]).values(password_hash=hashed)
            )
            connection.execute(delete(s.sessions).where(s.sessions.c.user_id == user["id"]))
            for related in (s.invitations, s.password_resets):
                connection.execute(
                    update(related)
                    .where(related.c.user_id == user["id"], related.c.used_at.is_(None))
                    .values(revoked_at=time.time())
                )
            return self.issue_session(connection, user) if kind == "invite" else None


async def account_changes(request, accounts, token, user_id):
    import asyncio

    from qa_job_bot.maintenance import active as maintenance_active
    from qa_job_bot.web_store import encode

    previous = None
    heartbeat = 0.0
    while not await request.is_disconnected():
        if maintenance_active(accounts.settings.data_dir):
            yield "event: maintenance\ndata: {}\n\n"
            return
        current = accounts.session(token)
        if current is None or current["user"].id != user_id:
            yield "event: session-ended\ndata: {}\n\n"
            return
        try:
            revision = PostgresStore(accounts.database, user_id).get_setting("revision", 0)
        except StoreError:
            yield "event: session-ended\ndata: {}\n\n"
            return
        if revision != previous:
            yield f"event: change\ndata: {encode({'revision': revision})}\n\n"
            previous = revision
        elif time.monotonic() - heartbeat > 20:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic()
        await asyncio.sleep(1)
