from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from qa_job_bot.models import Job, utcnow


class JobRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as source:
                columns = {row[1] for row in source.execute("PRAGMA table_info(jobs)")}
                if columns and "payload" not in columns:
                    backup = self.path.with_name(f"{self.path.name}.pre-web.bak")
                    if not backup.exists():
                        with closing(sqlite3.connect(backup)) as target:
                            source.backup(target)
                        backup.chmod(0o600)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    fingerprint TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT,
                    score INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    chat_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, fingerprint),
                    FOREIGN KEY (fingerprint) REFERENCES jobs(fingerprint)
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "payload" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN payload TEXT")

    def upsert_jobs(self, jobs: list[Job]) -> set[str]:
        now = utcnow().isoformat()
        inserted: set[str] = set()
        with self._connect() as connection:
            for job in jobs:
                payload = json.dumps(asdict(job), ensure_ascii=False, default=str)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO jobs (
                        fingerprint, source, source_id, title, company, location, url,
                        published_at, score, first_seen_at, last_seen_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.fingerprint,
                        job.source,
                        job.source_id,
                        job.title,
                        job.company,
                        job.location,
                        job.url,
                        job.published_at.isoformat() if job.published_at else None,
                        job.score,
                        now,
                        now,
                        payload,
                    ),
                )
                if cursor.rowcount:
                    inserted.add(job.fingerprint)
                else:
                    connection.execute(
                        """
                        UPDATE jobs SET source = ?, source_id = ?, url = ?, score = ?,
                            published_at = COALESCE(?, published_at), last_seen_at = ?, payload = ?
                        WHERE fingerprint = ?
                        """,
                        (
                            job.source,
                            job.source_id,
                            job.url,
                            job.score,
                            job.published_at.isoformat() if job.published_at else None,
                            now,
                            payload,
                            job.fingerprint,
                        ),
                    )
        return inserted

    def set_subscription(self, chat_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscriptions (chat_id, enabled, created_at) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET enabled = excluded.enabled
                """,
                (chat_id, int(enabled), utcnow().isoformat()),
            )

    def subscribed_chats(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM subscriptions WHERE enabled = 1 ORDER BY chat_id"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def undelivered(self, chat_id: int, jobs: list[Job], limit: int) -> list[Job]:
        if not jobs:
            return []
        with self._connect() as connection:
            delivered = {
                row["fingerprint"]
                for row in connection.execute(
                    "SELECT fingerprint FROM deliveries WHERE chat_id = ?", (chat_id,)
                ).fetchall()
            }
        return [job for job in jobs if job.fingerprint not in delivered][:limit]

    def mark_delivered(self, chat_id: int, jobs: list[Job]) -> None:
        if not jobs:
            return
        now = utcnow().isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO deliveries (chat_id, fingerprint, delivered_at)
                VALUES (?, ?, ?)
                """,
                [(chat_id, job.fingerprint, now) for job in jobs],
            )

    def stats(self) -> tuple[int, datetime | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, MAX(last_seen_at) AS last_seen FROM jobs"
            ).fetchone()
        parsed = datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None
        return int(row["count"]), parsed
