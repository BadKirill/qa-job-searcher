import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from qa_job_bot import pg_schema as s
from qa_job_bot.legacy_plan import build_plan, stable_id
from qa_job_bot.legacy_snapshot import (
    ImportFailure,
    decode_json,
    load_snapshot,
    read_private_file,
    sha256,
    write_private_file,
)
from qa_job_bot.web_store import digest

MARKER = "import-manifest.json"
SOURCE_KINDS = (
    "remotive",
    "himalayas",
    "weworkremotely",
    "remote_com",
    "hirify",
    "greenhouse",
    "lever",
    "ashby",
    "telegram",
)
ORDER = (
    "jobs",
    "job_identities",
    "job_matches",
    "user_jobs",
    "candidate_templates",
    "conversations",
    "messages",
    "tasks",
    "task_requests",
    "artifacts",
    "job_snapshots",
    "user_settings",
    "search_profiles",
    "user_sources",
    "telegram_bindings",
    "deliveries",
)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def marker_data(snapshot, user_id, telegram_chats):
    return {
        "format": 1,
        "source_hash": snapshot["manifest"]["source_hash"],
        "user_id": user_id,
        "telegram_chats": sorted(set(telegram_chats)),
        "files": snapshot["manifest"]["files"],
        "config": snapshot["manifest"]["config"],
    }


def verify_destination(target, marker):
    actual = decode_json(read_private_file(target, MARKER, 4 * 1024 * 1024))
    if actual != marker:
        raise ImportFailure("destination_conflict", "Директория принадлежит другому переносу.")
    for entries, prefix in ((marker["files"], ""), (marker["config"], "legacy-config/")):
        for entry in entries:
            data = read_private_file(target, prefix + entry["path"])
            if sha256(data) != entry["sha256"] or len(data) != entry["size"]:
                raise ImportFailure(
                    "destination_integrity", "Перенесённый файл изменён или неполон."
                )


def stage_files(snapshot, target, marker):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.import-", dir=target.parent))
    try:
        for entries, source_prefix, target_prefix in (
            (marker["files"], "data/", ""),
            (marker["config"], "config/", "legacy-config/"),
        ):
            for entry in entries:
                data = read_private_file(snapshot["root"], source_prefix + entry["path"])
                if sha256(data) != entry["sha256"] or len(data) != entry["size"]:
                    raise ImportFailure("snapshot_integrity", "Файл изменён во время переноса.")
                write_private_file(staging, target_prefix + entry["path"], data)
        write_private_file(staging, MARKER, json.dumps(marker, sort_keys=True).encode())
        for directory, _, _ in reversed(list(os.walk(staging))):
            sync_directory(directory)
        return staging
    except Exception:
        shutil.rmtree(staging)
        raise


def account_for_import(connection, user_id):
    user = (
        connection.execute(select(s.users).where(s.users.c.id == user_id).with_for_update())
        .mappings()
        .first()
    )
    if user is None or user["disabled"]:
        raise ImportFailure("invalid_owner", "Нужен существующий активный аккаунт владельца.")
    return user


def ensure_empty_owner(connection, user_id):
    ignored = {s.sessions.name, s.login_attempts.name, s.revisions.name}
    for table in s.metadata.sorted_tables:
        if (
            "user_id" in table.c
            and table.name not in ignored
            and connection.execute(
                select(table.c.user_id).where(table.c.user_id == user_id).limit(1)
            ).first()
        ):
            raise ImportFailure("target_not_empty", "Аккаунт уже содержит пользовательские данные.")


def equal_value(first, second):
    def canonical(value):
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        return value

    return digest(canonical(first)) == digest(canonical(second))


def write_rows(connection, plan):
    for kind in SOURCE_KINDS:
        connection.execute(
            insert(s.source_definitions)
            .values(kind=kind, payload={})
            .on_conflict_do_nothing(index_elements=["kind"])
        )
    reused = 0
    for name in ORDER:
        table = s.metadata.tables[name]
        for row in plan["rows"][name]:
            if name in {"jobs", "job_identities"}:
                key = "id" if name == "jobs" else "source_key"
                existing = (
                    connection.execute(select(table).where(table.c[key] == row[key]))
                    .mappings()
                    .first()
                )
                if existing is not None:
                    fields = set(row) - {"created_at", "last_seen_at"}
                    if any(not equal_value(row[field], existing[field]) for field in fields):
                        raise ImportFailure(
                            "public_identity_conflict",
                            "Общие идентичности конфликтуют с исходной историей.",
                        )
                    reused += name == "jobs"
                    continue
            connection.execute(table.insert().values(**row))
    return reused


def verify_rows(connection, plan):
    result = {}
    for name in ORDER:
        table = s.metadata.tables[name]
        expected = plan["rows"][name]
        for wanted in expected:
            conditions = [
                table.c[column.name] == wanted[column.name] for column in table.primary_key.columns
            ]
            actual = connection.execute(select(table).where(*conditions)).mappings().one()
            fields = set(wanted) - ({"created_at", "last_seen_at"} if name == "jobs" else set())
            if any(not equal_value(wanted[field], actual[field]) for field in fields):
                raise ImportFailure(
                    "reconciliation_failed", "Проверка сохранённых значений не пройдена."
                )
        result[name] = len(expected)
    return result


def insert_plan(connection, plan, user_id, source_hash):
    import_id = stable_id(source_hash, "import")
    connection.execute(
        s.import_runs.insert().values(
            id=import_id, user_id=user_id, source_hash=source_hash, payload={}
        )
    )
    reused = write_rows(connection, plan)
    for row in plan["archive"]:
        connection.execute(s.legacy_records.insert().values(import_id=import_id, **row))
    return import_id, reused


def restore_snapshot(
    snapshot_dir, database, user_id, target_dir, telegram_chats=(), check_only=False
):
    snapshot = load_snapshot(snapshot_dir)
    raw_target = Path(target_dir)
    if raw_target.is_symlink():
        raise ImportFailure("destination_conflict", "Директория назначения не может быть ссылкой.")
    target = raw_target.resolve()
    if target.is_relative_to(snapshot["root"].resolve()) or snapshot[
        "root"
    ].resolve().is_relative_to(target):
        raise ImportFailure(
            "destination_conflict", "Снимок и назначение должны быть раздельными директориями."
        )
    plan = build_plan(snapshot, user_id, target, telegram_chats)
    marker = marker_data(snapshot, user_id, telegram_chats)
    source_hash = snapshot["manifest"]["source_hash"]
    staging, published, committed = None, False, False
    commit_started = False
    try:
        with database.transaction() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(1853392743)"))
            user = account_for_import(connection, user_id)
            previous = (
                connection.execute(
                    select(s.import_runs).where(s.import_runs.c.source_hash == source_hash)
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["user_id"] != user_id:
                    raise ImportFailure(
                        "owner_mismatch", "Этот снимок уже принадлежит другому аккаунту."
                    )
                if previous["payload"]["target_dir"] != str(target):
                    raise ImportFailure(
                        "destination_conflict", "Перенос уже связан с другой директорией."
                    )
                verify_destination(target, marker)
                return {**previous["payload"]["report"], "already_imported": True}
            ensure_empty_owner(connection, user_id)
            if plan["password_hash"] and user["password_hash"] not in (None, plan["password_hash"]):
                raise ImportFailure("password_conflict", "Аккаунт уже имеет другой пароль.")
            if target.exists():
                verify_destination(target, marker)
            if check_only:
                preview = connection.begin_nested()
                try:
                    _, reused = insert_plan(connection, plan, user_id, source_hash)
                    verified = verify_rows(connection, plan)
                finally:
                    preview.rollback()
                return {
                    **plan["report"],
                    "verified_counts": verified,
                    "reused_public_jobs": reused,
                    "checked": True,
                    "already_imported": False,
                }
            if not target.exists():
                staging = stage_files(snapshot, target, marker)
            import_id, reused = insert_plan(connection, plan, user_id, source_hash)
            target_sessions = connection.execute(
                delete(s.sessions).where(s.sessions.c.user_id == user_id)
            ).rowcount
            connection.execute(
                delete(s.login_attempts).where(s.login_attempts.c.user_id == user_id)
            )
            connection.execute(
                insert(s.revisions)
                .values(user_id=user_id, revision=plan["revision"] + 1)
                .on_conflict_do_update(
                    index_elements=["user_id"], set_={"revision": plan["revision"] + 1}
                )
            )
            if plan["password_hash"]:
                connection.execute(
                    update(s.users)
                    .where(s.users.c.id == user_id)
                    .values(password_hash=plan["password_hash"])
                )
            report = {
                **plan["report"],
                "verified_counts": verify_rows(connection, plan),
                "archive_records": len(plan["archive"]),
                "reused_public_jobs": reused,
                "target_sessions_revoked": target_sessions,
                "already_imported": False,
            }
            connection.execute(
                update(s.import_runs)
                .where(s.import_runs.c.id == import_id)
                .values(payload={"target_dir": str(target), "report": report})
            )
            if staging is not None:
                if target.exists():
                    raise ImportFailure(
                        "destination_conflict", "Назначение появилось во время переноса."
                    )
                staging.rename(target)
                staging, published = None, True
                sync_directory(target.parent)
            verify_destination(target, marker)
            commit_started = True
        committed = True
        return report
    except SQLAlchemyError:
        if commit_started:
            raise ImportFailure(
                "commit_outcome_unknown",
                "Исход commit неизвестен. Повторите ту же команду для сверки.",
            ) from None
        raise ImportFailure(
            "database_rejected", "База отклонила перенос. Данные не опубликованы."
        ) from None
    except OSError:
        if commit_started:
            raise ImportFailure(
                "commit_outcome_unknown",
                "Исход commit неизвестен. Повторите ту же команду для сверки.",
            ) from None
        raise ImportFailure(
            "filesystem_failure", "Не удалось опубликовать приватные файлы."
        ) from None
    finally:
        if staging is not None:
            shutil.rmtree(staging)
        if published and not committed and not commit_started:
            verify_destination(target, marker)
            shutil.rmtree(target)
            sync_directory(target.parent)
