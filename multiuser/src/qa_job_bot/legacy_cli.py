import argparse
import json
import unicodedata
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from qa_job_bot import pg_schema as s
from qa_job_bot.legacy_import import restore_snapshot
from qa_job_bot.legacy_snapshot import ImportFailure, create_snapshot
from qa_job_bot.pg_database import Database


def bootstrap_owner(database, login, display_name):
    login = unicodedata.normalize("NFKC", login).strip().casefold()
    if not login or not display_name.strip():
        raise ImportFailure("invalid_owner", "Нужны логин и имя владельца.")
    with database.transaction() as connection:
        connection.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
        if connection.execute(select(func.count()).select_from(s.users)).scalar_one():
            raise ImportFailure("bootstrap_completed", "Первоначальный владелец уже создан.")
        user_id = str(uuid4())
        connection.execute(
            s.users.insert().values(
                id=user_id, login=login, display_name=display_name.strip(), role="admin"
            )
        )
    return {"user_id": user_id, "password_setup": "private_web_setup_token"}


def parser():
    cli = argparse.ArgumentParser(prog="qa-job-import")
    commands = cli.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--database", required=True)
    snapshot.add_argument("--data-dir", required=True)
    snapshot.add_argument("--config-dir", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.add_argument("--legacy-data-root")
    for command in ("check", "restore"):
        restore = commands.add_parser(command)
        restore.add_argument("--snapshot", required=True)
        restore.add_argument("--owner-id", required=True)
        restore.add_argument("--target-dir", required=True)
        restore.add_argument("--telegram-chat", action="append", type=int, default=[])
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--login", required=True)
    bootstrap.add_argument("--display-name", required=True)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    database = None
    try:
        if args.command == "snapshot":
            snapshot = create_snapshot(
                args.database, args.data_dir, args.config_dir, args.output, args.legacy_data_root
            )
            result = {
                "source_hash": snapshot["manifest"]["source_hash"],
                "source_counts": {key: len(rows) for key, rows in snapshot["tables"].items()},
                "files": len(snapshot["manifest"]["files"]),
            }
        else:
            database = Database.from_env()
            if args.command == "bootstrap":
                result = bootstrap_owner(database, args.login, args.display_name)
            else:
                result = restore_snapshot(
                    args.snapshot,
                    database,
                    args.owner_id,
                    args.target_dir,
                    args.telegram_chat,
                    check_only=args.command == "check",
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ImportFailure, SQLAlchemyError, OSError, KeyError, ValueError) as error:
        print(
            json.dumps(
                {"error": error.code if isinstance(error, ImportFailure) else "import_unavailable"},
                sort_keys=True,
            )
        )
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
