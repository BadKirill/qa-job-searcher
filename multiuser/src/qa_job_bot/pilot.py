import argparse
import hashlib
import json
import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from qa_job_bot import pg_schema as s
from qa_job_bot.openai_config import private_text
from qa_job_bot.pg_database import Database
from qa_job_bot.web_store import StoreError

CHECKS = (
    "owner_history",
    "search_professions",
    "personal_states",
    "candidate_templates",
    "conversations",
    "documents",
    "access_isolation",
)


def lock(connection):
    connection.execute(
        insert(s.runtime_control).values(key="pilot", payload={}).on_conflict_do_nothing()
    )
    return dict(
        connection.execute(
            select(s.runtime_control.c.payload)
            .where(s.runtime_control.c.key == "pilot")
            .with_for_update()
        ).scalar_one()
    )


def save(connection, state):
    connection.execute(
        s.runtime_control.update().where(s.runtime_control.c.key == "pilot").values(payload=state)
    )


def reserved(connection):
    return connection.execute(
        select(func.count()).select_from(s.users).where(s.users.c.disabled.is_(False))
    ).scalar_one()


def guard_admission(connection):
    state = lock(connection)
    limit = state.get("limit")
    if limit is not None and reserved(connection) >= limit:
        message = (
            "Сначала завершите проверку владельца и второго участника."
            if state.get("phase") == "pair"
            else "Все 20 мест закрытого пилота заняты."
        )
        raise StoreError(message, 409)


class Pilot:
    def __init__(self, database):
        self.database = database

    def status(self):
        with self.database.transaction() as connection:
            state = (
                connection.execute(
                    select(s.runtime_control.c.payload).where(s.runtime_control.c.key == "pilot")
                ).scalar_one_or_none()
                or {}
            )
            count = reserved(connection)
        return {
            "phase": state.get("phase", "not_started"),
            "limit": state.get("limit"),
            "reserved": count,
            "pair_verified": bool(state.get("evidence_hash")),
        }

    def begin(self, owner_id):
        with self.database.transaction() as connection:
            state = lock(connection)
            if state:
                if state.get("phase") != "pair" or state.get("owner_id") != owner_id:
                    raise StoreError(
                        "Пилот уже настроен. Автоматическое повторное переключение запрещено.", 409
                    )
            else:
                owner = (
                    connection.execute(
                        select(s.users).where(
                            s.users.c.id == owner_id,
                            s.users.c.role == "admin",
                            s.users.c.disabled.is_(False),
                        )
                    )
                    .mappings()
                    .first()
                )
                if owner is None or reserved(connection) != 1:
                    raise StoreError(
                        "Начните пилот с единственного активного владельца-администратора.", 409
                    )
                save(connection, {"phase": "pair", "owner_id": owner_id, "limit": 2})
        return self.status()

    def expand(self, evidence):
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"owner_id", "tester_id", "source_hash", "checks"}
            or not isinstance(evidence["checks"], dict)
            or set(evidence["checks"]) != set(CHECKS)
            or any(evidence["checks"][name] is not True for name in CHECKS)
            or any(
                not isinstance(evidence[key], str) or not evidence[key]
                for key in ("owner_id", "tester_id", "source_hash")
            )
            or not re.fullmatch(r"[a-f0-9]{64}", evidence["source_hash"])
        ):
            raise StoreError("Нужен полный отчёт проверки двух аккаунтов.", 422)
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        with self.database.transaction() as connection:
            state = lock(connection)
            if state.get("phase") != "pair":
                if state.get("phase") != "cohort" or state.get("evidence_hash") != digest:
                    raise StoreError("Сначала включите проверку двух аккаунтов.", 409)
            else:
                users = (
                    connection.execute(select(s.users).where(s.users.c.disabled.is_(False)))
                    .mappings()
                    .all()
                )
                ids = {row["id"] for row in users if row["password_hash"] is not None}
                imported = connection.execute(
                    select(s.import_runs.c.source_hash).where(
                        s.import_runs.c.user_id == evidence["owner_id"],
                        s.import_runs.c.source_hash == evidence["source_hash"],
                    )
                ).first()
                if (
                    state["owner_id"] != evidence["owner_id"]
                    or evidence["owner_id"] == evidence["tester_id"]
                    or ids != {evidence["owner_id"], evidence["tester_id"]}
                    or len(users) != 2
                    or imported is None
                ):
                    raise StoreError(
                        "Отчёт не соответствует импортированной истории и двум активным аккаунтам.",
                        409,
                    )
                save(connection, {**state, "phase": "cohort", "limit": 20, "evidence_hash": digest})
        return self.status()


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    begin = commands.add_parser("begin")
    begin.add_argument("--owner-id", required=True)
    expand = commands.add_parser("expand")
    expand.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    database = Database.from_env()
    try:
        pilot = Pilot(database)
        if arguments.command == "begin":
            result = pilot.begin(arguments.owner_id)
        elif arguments.command == "expand":
            result = pilot.expand(json.loads(private_text(arguments.evidence, 4096)))
        else:
            result = pilot.status()
        print(json.dumps(result))
    except (StoreError, OSError, ValueError, KeyError, SQLAlchemyError) as error:
        print(
            json.dumps(
                {
                    "error": "pilot_unavailable",
                    "detail": str(error)
                    if isinstance(error, StoreError)
                    else "Проверьте подключение, аргументы и приватный отчёт.",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    finally:
        database.close()


if __name__ == "__main__":
    main()
