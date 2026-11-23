import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from qa_job_bot import pg_schema as s
from qa_job_bot.maintenance import active
from qa_job_bot.pg_access import serialize
from qa_job_bot.pg_database import REVISION, Database
from qa_job_bot.runtime_backup import inventory
from qa_job_bot.web_store import StoreError


def fingerprint(database, data_dir):
    files = inventory(data_dir)
    digest, counts = hashlib.sha256(), {}
    with database.engine.connect().execution_options(
        isolation_level="REPEATABLE READ"
    ) as connection:
        for table in s.metadata.sorted_tables:
            if table.name == "runtime_control":
                continue
            rows = connection.execute(select(table).order_by(*table.primary_key.columns)).mappings()
            counts[table.name] = 0
            digest.update(table.name.encode())
            for row in rows:
                digest.update(
                    json.dumps(serialize(dict(row)), sort_keys=True, ensure_ascii=False).encode()
                )
                counts[table.name] += 1
    if inventory(data_dir) != files:
        raise StoreError("Файлы изменились во время проверки. Остановите все процессы записи.", 409)
    return {
        "database_hash": digest.hexdigest(),
        "files_hash": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "row_counts": counts,
        "revision": database.revision(),
        "data_root": str(Path(data_dir).absolute()),
    }


class Cutover:
    def __init__(self, database, data_dir):
        self.database, self.data_dir = database, Path(data_dir)

    def record(self, *, services_stopped=False):
        if not services_stopped or not active(self.data_dir):
            raise StoreError(
                "Включите обслуживание и остановите все сервисы перед фиксацией импорта.", 409
            )
        observed = fingerprint(self.database, self.data_dir)
        if observed["revision"] != REVISION:
            raise StoreError("Нужна текущая миграция PostgreSQL.", 409)
        with self.database.transaction() as connection:
            connection.execute(
                insert(s.runtime_control)
                .values(key="cutover-baseline", payload={})
                .on_conflict_do_nothing()
            )
            previous = connection.execute(
                select(s.runtime_control.c.payload)
                .where(s.runtime_control.c.key == "cutover-baseline")
                .with_for_update()
            ).scalar_one()
            if previous and previous != observed:
                raise StoreError(
                    "После импорта появились изменения. "
                    "Исходная граница отката не перезаписывается.",
                    409,
                )
            state = (
                connection.execute(
                    select(s.runtime_control.c.payload).where(s.runtime_control.c.key == "pilot")
                ).scalar_one_or_none()
                or {}
            )
            imported = connection.execute(
                select(s.import_runs.c.id).where(
                    s.import_runs.c.user_id == state.get("owner_id"),
                    s.import_runs.c.payload["target_dir"].astext == observed["data_root"],
                )
            ).first()
            running = connection.execute(
                select(func.count())
                .select_from(s.tasks)
                .where(s.tasks.c.status.in_(["running", "verifying", "rendering"]))
            ).scalar_one()
            if imported is None or running or state.get("phase") != "pair":
                raise StoreError(
                    "Сверьте импорт владельца и незавершённые задания перед пилотом.", 409
                )
            connection.execute(
                s.runtime_control.update()
                .where(s.runtime_control.c.key == "cutover-baseline")
                .values(payload=observed)
            )
        return {"recorded": True, "revision": observed["revision"], "files_preserved": True}

    def rollback_status(self, *, services_stopped=False):
        with self.database.transaction() as connection:
            baseline = connection.execute(
                select(s.runtime_control.c.payload).where(
                    s.runtime_control.c.key == "cutover-baseline"
                )
            ).scalar_one_or_none()
        if not baseline:
            raise StoreError("Граница импорта ещё не зафиксирована.", 409)
        if baseline["data_root"] != str(self.data_dir.absolute()):
            raise StoreError("Проверяйте тот же каталог данных, который был импортирован.", 409)
        observed = fingerprint(self.database, self.data_dir)
        database_changed = any(
            observed[key] != baseline[key] for key in ("database_hash", "revision")
        )
        files_changed = observed["files_hash"] != baseline["files_hash"]
        paused = active(self.data_dir)
        unchanged = not database_changed and not files_changed
        return {
            "database_changed": database_changed,
            "files_changed": files_changed,
            "maintenance": paused,
            "matches_import_baseline": unchanged,
            "legacy_restart_allowed": unchanged and paused and services_stopped,
            "preserve_postgres_and_files": not unchanged,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("record", "rollback-status"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("WEB_DATA_DIR", "data")))
    parser.add_argument("--services-stopped", action="store_true")
    arguments = parser.parse_args()
    database = Database.from_env()
    try:
        cutover = Cutover(database, arguments.data_dir)
        operation = cutover.record if arguments.command == "record" else cutover.rollback_status
        print(json.dumps(operation(services_stopped=arguments.services_stopped)))
    except (OSError, ValueError, KeyError, StoreError, SQLAlchemyError) as error:
        print(
            json.dumps(
                {
                    "error": "cutover_unavailable",
                    "detail": str(error)
                    if isinstance(error, StoreError)
                    else "Проверьте базу, каталог данных и сохранённую границу импорта.",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    finally:
        database.close()


if __name__ == "__main__":
    main()
