import os

from qa_job_bot.pg_database import Database
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web_store import WebStore


def open_store(settings):
    if not os.getenv("DATABASE_URL"):
        return WebStore(settings.data_dir / "jobs.sqlite3")
    user_id = os.getenv("WEB_OWNER_ID", "").strip()
    if not user_id:
        raise ValueError("WEB_OWNER_ID is required until per-account authentication is enabled")
    database = Database.from_env()
    try:
        store = PostgresStore(database, user_id)
        store.initialize()
        return store
    except Exception:
        database.close()
        raise


def close_store(store):
    if isinstance(store, PostgresStore):
        store.database.close()
