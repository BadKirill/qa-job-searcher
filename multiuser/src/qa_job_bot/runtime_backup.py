import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from sqlalchemy import select, text

from qa_job_bot import pg_schema as s
from qa_job_bot.operations import ACTIVE
from qa_job_bot.personal_files import checked_path
from qa_job_bot.pg_database import REVISION, Database


class BackupError(ValueError):
    pass


def file_digest(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("Backup accepts regular files only")
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"sha256": digest, "size": info.st_size}


def inventory(root):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise BackupError("Data root must be a regular directory")
    result = {}
    for path in sorted(root.rglob("*")):
        checked_path(root, path)
        if path.is_dir():
            continue
        result[path.relative_to(root).as_posix()] = file_digest(path)
    return result


def copy_files(source, target, entries):
    for name, expected in entries.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise BackupError("Invalid backup file path")
        original, destination = checked_path(source, source / relative), target / relative
        parent = target
        for part in relative.parts[:-1]:
            parent /= part
            parent.mkdir(exist_ok=True, mode=0o700)
        descriptor = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as outgoing:
            destination.chmod(0o600)
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise BackupError("Backup accepts regular files only")
            shutil.copyfileobj(incoming, outgoing)
        if file_digest(destination) != expected:
            raise BackupError("File changed during backup or backup is damaged")


def run_pg(database, program, arguments):
    url = database.engine.url
    environment = {
        **os.environ,
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        "PGCONNECT_TIMEOUT": "5",
    }
    for name in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        if name in url.query:
            environment["PG" + name.upper()] = url.query[name]
    try:
        subprocess.run(
            [program, *arguments],
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        raise BackupError("PostgreSQL utility failed; backup/restore did not complete") from None


def require_offline(database, stopped):
    if not stopped:
        raise BackupError("Stop API, scheduler, all workers and Telegram before this operation")
    with database.transaction() as connection:
        active = connection.execute(
            select(s.tasks.c.id).where(
                s.tasks.c.status.in_(ACTIVE), s.tasks.c.lease_until > time.time()
            )
        ).first()
    if active:
        raise BackupError("Wait for active task leases to expire before backup")


def backup(database, data_dir, destination, *, services_stopped=False):
    require_offline(database, services_stopped)
    if database.revision() != REVISION:
        raise BackupError("Migrate the source database before backup")
    data_dir, destination = Path(data_dir).absolute(), Path(destination).absolute()
    if destination.is_relative_to(data_dir):
        raise BackupError("Backup destination must be outside the data directory")
    entries = inventory(data_dir)
    destination.mkdir(mode=0o700)
    try:
        files = destination / "files"
        files.mkdir(mode=0o700)
        copy_files(data_dir, files, entries)
        dump = destination / "postgres.dump"
        dump.touch(mode=0o600)
        run_pg(
            database,
            "pg_dump",
            [
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--schema=" + database.schema,
                "--file=" + str(dump),
            ],
        )
        if inventory(data_dir) != entries:
            raise BackupError("Data changed during backup; stop all writers and retry")
        manifest = {
            "format": 1,
            "revision": REVISION,
            "schema": database.schema,
            "data_root": str(data_dir),
            "files": entries,
            "dump": file_digest(dump),
        }
        output = destination / "manifest.json"
        with output.open("x") as stream:
            output.chmod(0o600)
            json.dump(manifest, stream, sort_keys=True)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {"files": len(entries), "revision": REVISION}


def restore(database, data_dir, source, *, services_stopped=False):
    if not services_stopped:
        raise BackupError("Stop every service before restore")
    source, data_dir = Path(source).absolute(), Path(data_dir).absolute()
    checked_path(source, source / "manifest.json")
    manifest_path = source / "manifest.json"
    if manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise BackupError("Backup manifest is too large")
    try:
        manifest = json.loads(manifest_path.read_text())
        valid = (
            manifest["format"] == 1
            and manifest["revision"] == REVISION
            and manifest["schema"] == database.schema
            and manifest["data_root"] == str(data_dir)
            and isinstance(manifest["files"], dict)
        )
    except (ValueError, TypeError, KeyError):
        raise BackupError("Invalid backup manifest") from None
    if not valid:
        raise BackupError("Backup format, revision, schema or mounted data path does not match")
    if data_dir.is_symlink() or (
        data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir()))
    ):
        raise BackupError("Restore requires an empty data directory")
    if inventory(source / "files") != manifest["files"]:
        raise BackupError("Backup files are missing, changed or unexpected")
    dump = checked_path(source, source / "postgres.dump")
    if file_digest(dump) != manifest["dump"]:
        raise BackupError("Backup database checksum does not match")
    with database.transaction() as connection:
        populated = connection.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"
            )
        ).scalar_one()
    if populated:
        raise BackupError("Restore requires an empty PostgreSQL database")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_dir) as staged:
        staged = Path(staged)
        copy_files(source / "files", staged, manifest["files"])
        run_pg(
            database,
            "pg_restore",
            [
                "--exit-on-error",
                "--single-transaction",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--dbname=" + database.engine.url.database,
                str(dump),
            ],
        )
        if database.revision() != REVISION:
            raise BackupError("Restored database revision is unexpected")
        for child in staged.iterdir():
            child.rename(data_dir / child.name)
    return {"files": len(manifest["files"]), "revision": REVISION}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("WEB_DATA_DIR", "data")))
    parser.add_argument("--services-stopped", action="store_true")
    arguments = parser.parse_args()
    database = Database.from_env()
    try:
        operation = backup if arguments.action == "backup" else restore
        print(
            json.dumps(
                operation(
                    database,
                    arguments.data_dir,
                    arguments.directory,
                    services_stopped=arguments.services_stopped,
                )
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
