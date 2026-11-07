import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qa_job_bot.models import Job
from qa_job_bot.repository import JobRepository


def test_repository_deduplicates_and_tracks_delivery(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    job = Job(
        source="Remotive",
        source_id="42",
        title="QA Lead",
        company="Acme",
        location="Europe",
        url="https://example.com/42",
        score=75,
    )
    assert repository.upsert_jobs([job]) == {job.fingerprint}
    assert repository.upsert_jobs([job]) == set()

    repository.set_subscription(123, True)
    assert repository.subscribed_chats() == [123]
    assert repository.undelivered(123, [job], 10) == [job]
    repository.mark_delivered(123, [job])
    assert repository.undelivered(123, [job], 10) == []
    count, last_seen = repository.stats()
    assert count == 1
    assert last_seen is not None


@pytest.mark.parametrize("existing_backup", [True, False])
def test_legacy_database_migration_preserves_history_and_consistent_backup(
    tmp_path: Path,
    existing_backup: bool,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    backup = tmp_path / "legacy.sqlite3.pre-web.bak"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE jobs (fingerprint TEXT PRIMARY KEY,source TEXT NOT NULL,
                source_id TEXT NOT NULL,title TEXT NOT NULL,company TEXT NOT NULL,
                location TEXT NOT NULL,url TEXT NOT NULL,published_at TEXT,score INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
            CREATE TABLE subscriptions (chat_id INTEGER PRIMARY KEY,enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE deliveries (chat_id INTEGER NOT NULL,fingerprint TEXT NOT NULL,
                delivered_at TEXT NOT NULL,PRIMARY KEY(chat_id,fingerprint));
            INSERT INTO jobs VALUES ('legacy','source','1','QA','Acme','Europe',
                'https://jobs.example/1',NULL,70,'2026-01-01','2026-01-02');
            INSERT INTO subscriptions VALUES (123,1,'2026-01-01');
            INSERT INTO deliveries VALUES (123,'legacy','2026-01-02');
        """)
    if existing_backup:
        with closing(sqlite3.connect(path)) as original, closing(sqlite3.connect(backup)) as copied:
            original.backup(copied)
    repository = JobRepository(path)
    repository.initialize()
    with closing(sqlite3.connect(backup)) as original:
        assert original.execute("SELECT fingerprint FROM jobs").fetchall() == [("legacy",)]
        assert "payload" not in {row[1] for row in original.execute("PRAGMA table_info(jobs)")}
        assert original.execute("SELECT chat_id FROM deliveries").fetchall() == [(123,)]
    if not existing_backup:
        assert backup.stat().st_mode & 0o777 == 0o600
    assert repository.stats() == (1, datetime(2026, 1, 2))
    assert repository.subscribed_chats() == [123]
    with repository._connect() as db:
        assert db.execute("SELECT payload FROM jobs").fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
    size, modified = backup.stat().st_size, backup.stat().st_mtime_ns
    repository.initialize()
    assert (backup.stat().st_size, backup.stat().st_mtime_ns) == (size, modified)


def test_repository_updates_payload_preserves_first_seen_and_delivery(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "nested" / "state.sqlite3")
    repository.initialize()
    job = Job(
        "source",
        "1",
        "QA",
        "Acme",
        "Europe",
        "https://jobs.example/1",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        description="Original",
    )
    repository.upsert_jobs([job])
    repository.mark_delivered(123, [job])
    with repository._connect() as db:
        first_seen = db.execute("SELECT first_seen_at FROM jobs").fetchone()[0]
    changed = replace(
        job,
        score=99,
        salary="EUR 5000",
        description="Full updated text",
        url="https://jobs.example/new",
    )
    assert repository.upsert_jobs([changed]) == set()
    with repository._connect() as db:
        row = db.execute("SELECT * FROM jobs").fetchone()
        assert row["source"] == "source"
        assert row["url"] == "https://jobs.example/new"
        assert row["first_seen_at"] == first_seen
        assert json.loads(row["payload"])["description"] == "Full updated text"
        assert json.loads(row["payload"])["salary"] == "EUR 5000"
    assert repository.undelivered(123, [changed], 10) == []
    assert repository.undelivered(456, [changed], 0) == []
    assert repository.undelivered(123, [], 10) == []
    repository.mark_delivered(123, [])
    repository.set_subscription(123, True)
    repository.set_subscription(123, False)
    assert repository.subscribed_chats() == []


def test_collision_gets_distinct_stable_id_and_preserves_legacy_history(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    repository = JobRepository(path)
    repository.initialize()
    original = Job("board", "one", "QA Engineer", "Acme", "Europe", "https://jobs.test/one")
    repository.upsert_jobs([original])
    original_id = original.fingerprint
    repository.mark_delivered(123, [original])

    other = Job("board", "two", "QA Engineer", "Acme", "Europe", "https://jobs.test/two")
    repository.resolve_identities([other])
    assert other.fingerprint != original_id
    assert other.legacy_fingerprint == original_id
    repository.upsert_jobs([other])
    assert repository.stats()[0] == 2
    assert repository.undelivered(123, [original, other], 10) == [other]

    restarted = JobRepository(path)
    restarted.initialize()
    again = Job("board", "two", "QA Engineer", "Acme", "Europe", "https://jobs.test/two")
    restarted.resolve_identities([again])
    assert again.fingerprint == other.fingerprint
    changed = replace(again, title="Senior QA Engineer", location="Remote Europe")
    restarted.upsert_jobs([changed])
    assert changed.fingerprint == other.fingerprint
    assert restarted.stats()[0] == 2


def test_same_url_from_another_source_keeps_one_listing(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    original = Job("board", "one", "QA Engineer", "Acme", "Europe", "https://jobs.test/one")
    mirror = replace(original, source="mirror", source_id="other")
    repository.resolve_identities([original, mirror])
    assert original.fingerprint == mirror.fingerprint
    repository.upsert_jobs([original, mirror])
    assert repository.stats()[0] == 1


def test_concurrent_colliding_listings_reserve_distinct_persistent_ids(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    repository = JobRepository(path)
    repository.initialize()
    jobs = [
        Job("board", str(index), "QA Engineer", "Acme", "Europe", f"https://jobs.test/{index}")
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda listing: repository.upsert_jobs([listing]), jobs))
    assert len({listing.fingerprint for listing in jobs}) == 2
    assert repository.stats()[0] == 2
    restarted = JobRepository(path)
    restarted.initialize()
    again = [
        Job("board", str(index), "QA Engineer", "Acme", "Europe", f"https://jobs.test/{index}")
        for index in range(2)
    ]
    restarted.resolve_identities(again)
    assert [listing.fingerprint for listing in again] == [listing.fingerprint for listing in jobs]


def test_existing_empty_database_initializes_without_legacy_backup(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    path.touch()
    repository = JobRepository(path)
    repository.initialize()
    assert repository.stats() == (0, None)
    assert not path.with_name(path.name + ".pre-web.bak").exists()
