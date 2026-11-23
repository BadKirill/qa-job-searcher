import os
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import text, update

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_database import Database
from qa_job_bot.pg_store import PostgresStore


@contextmanager
def postgres_database():
    url = os.environ.get("QA_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("QA_TEST_DATABASE_URL must point to a PostgreSQL test database")
    schema = f"qa_test_{uuid4().hex}"
    database = Database(url, schema)
    try:
        database.migrate()
        yield database
    finally:
        drop_test_schema(database)
        database.close()


def drop_test_schema(database):
    if not database.schema.startswith("qa_test_"):
        raise ValueError("Only temporary test schemas can be removed")
    with database.engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{database.schema}" CASCADE'))


def personal_store(database, login="synthetic-owner"):
    user = database.create_user(login, login)
    store = PostgresStore(database, user["id"])
    store.initialize()
    return store


def tamper_task(store, task_id, **values):
    values = deepcopy(values)
    if "context" in values.get("payload", {}):
        values["payload"]["context"]["owner_id"] = store.user_id
    with store.database.transaction() as connection:
        connection.execute(
            update(s.tasks)
            .where(s.tasks.c.id == task_id, s.tasks.c.user_id == store.user_id)
            .values(**values)
        )
