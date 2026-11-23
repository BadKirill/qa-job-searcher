import os
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from qa_job_bot.pg_schema import users
from qa_job_bot.web_store import StoreError

REVISION = "0007_shared_runtime"


class Database:
    def __init__(self, url: str, schema: str = "public"):
        address = make_url(url)
        if address.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("PostgreSQL with psycopg is required")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("Invalid database schema")
        self.schema = schema
        self.engine = create_engine(
            address.set(drivername="postgresql+psycopg"),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": f"-csearch_path={schema} -ctimezone=UTC",
            },
        )

    @classmethod
    def from_env(cls):
        return cls(os.environ["DATABASE_URL"], os.getenv("DATABASE_SCHEMA", "public"))

    def migration_config(self, connection):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        config.attributes["connection"] = connection
        return config

    def migrate(self):
        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(1853392741)"))
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            command.upgrade(self.migration_config(connection), "head")

    def revision(self):
        with self.engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return None
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()

    def create_user(self, login: str, display_name: str, *, user_id=None, role="user"):
        normalized = unicodedata.normalize("NFKC", login).strip().casefold()
        if not normalized or not display_name.strip() or role not in {"user", "admin"}:
            raise StoreError("Некорректный аккаунт.", 422)
        data = {
            "id": user_id or str(uuid4()),
            "login": normalized,
            "display_name": display_name.strip(),
            "role": role,
        }
        with self.engine.begin() as connection:
            return dict(
                connection.execute(users.insert().values(**data).returning(users)).mappings().one()
            )

    def user(self, user_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(users).where(users.c.id == user_id)).mappings().first()
        if row is None or row["disabled"]:
            raise StoreError("Аккаунт недоступен.", 403)
        return dict(row)

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            yield connection

    def close(self):
        self.engine.dispose()


def main():
    database = Database.from_env()
    try:
        database.migrate()
        print(f"Database revision: {database.revision()}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
