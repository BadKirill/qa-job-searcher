import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from qa_job_bot import pg_schema as s
from qa_job_bot import runtime_backup as module
from qa_job_bot.pg_database import REVISION, Database


@contextmanager
def empty_database(source, schema=None):
    name = "qa_restore_" + uuid4().hex
    with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    target = Database(source.engine.url.set(database=name), schema or source.schema)
    try:
        yield target
    finally:
        target.close()
        with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def container_pg(database, program, arguments):
    container = os.getenv("QA_PG_CONTAINER", "job-searcher-tests-postgres-1")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    command = [
        "docker",
        "--host=unix:///var/run/docker.sock",
        "exec",
        "-i",
        container,
        program,
        "--username=" + database.engine.url.username,
    ]
    if program == "pg_dump":
        destination = next(
            item.removeprefix("--file=") for item in arguments if item.startswith("--file=")
        )
        args = [item for item in arguments if not item.startswith("--file=")]
        with Path(destination).open("wb") as output:
            subprocess.run(
                [*command, *args, "--dbname=" + database.engine.url.database],
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
                timeout=30,
            )
    else:
        subprocess.run(
            [*command, *arguments[:-1]],
            input=Path(arguments[-1]).read_bytes(),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )


def fake_dump(database, program, arguments):
    path = next(item.removeprefix("--file=") for item in arguments if item.startswith("--file="))
    Path(path).write_bytes(b"synthetic-unit-dump")


@pytest.fixture
def saved_backup(pg_database, tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "private.txt").write_text("synthetic private content")
    output = tmp_path / "backup"
    monkeypatch.setattr(module, "run_pg", fake_dump)
    module.backup(pg_database, root, output, services_stopped=True)
    return root, output


def test_postgresql_backup_restores_accounts_and_private_files_into_an_empty_database(
    pg_store, tmp_path, monkeypatch
):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    files = root / "users" / "synthetic"
    files.mkdir(parents=True)
    (files / "cv.pdf").write_bytes(b"synthetic PDF backup content")
    pg_store.set_setting("synthetic_artifact_path", str(files / "cv.pdf"))
    output = tmp_path / "backup"
    monkeypatch.setattr(module, "run_pg", container_pg)
    result = module.backup(pg_store.database, root, output, services_stopped=True)
    assert result == {"files": 1, "revision": REVISION}
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.rglob("*"))
    original = tmp_path / "original-data"
    root.rename(original)
    with empty_database(pg_store.database) as target:
        restored = module.restore(target, root, output, services_stopped=True)
        assert restored == result and target.revision() == REVISION
        assert target.user(pg_store.user_id)["login"] == "synthetic-owner"
        with target.transaction() as connection:
            path = connection.execute(
                select(s.settings.c.value).where(
                    s.settings.c.user_id == pg_store.user_id,
                    s.settings.c.key == "synthetic_artifact_path",
                )
            ).scalar_one()
        assert Path(path).read_bytes() == b"synthetic PDF backup content"
        assert module.inventory(original) == module.inventory(root)


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077])
def test_nested_backup_directories_are_private_regardless_of_umask(tmp_path, mask):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    nested = source / "users" / "synthetic" / "documents"
    nested.mkdir(parents=True)
    (nested / "cv.pdf").write_bytes(b"synthetic document")
    entries = module.inventory(source)
    previous = os.umask(mask)
    try:
        module.copy_files(source, target, entries)
    finally:
        os.umask(previous)
    assert module.inventory(target) == entries
    assert all(path.stat().st_mode & 0o077 == 0 for path in target.rglob("*"))


def test_public_schema_restores_into_an_empty_mounted_directory(pg_database, tmp_path, monkeypatch):
    root, output = tmp_path / "mounted-data", tmp_path / "backup"
    root.mkdir(mode=0o700)
    marker = root / "synthetic.txt"
    marker.write_text("public schema backup rehearsal")
    monkeypatch.setattr(module, "run_pg", container_pg)
    with empty_database(pg_database, schema="public") as original:
        original.migrate()
        user = original.create_user("public-owner", "Synthetic Owner")
        module.backup(original, root, output, services_stopped=True)
        marker.unlink()
        with empty_database(original) as target:
            module.restore(target, root, output, services_stopped=True)
            assert target.user(user["id"])["login"] == "public-owner"
            assert marker.read_text() == "public schema backup rehearsal"


def test_backup_requires_offline_and_rejects_destination_inside_data(pg_database, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(module.BackupError, match="Stop API"):
        module.backup(pg_database, root, tmp_path / "backup")
    with pytest.raises(module.BackupError, match="outside"):
        module.backup(pg_database, root, root / "backup", services_stopped=True)


def test_backup_rejects_active_leases_and_old_revision(pg_store, tmp_path, monkeypatch):
    from test_shared_runtime import claimed

    claimed(pg_store)
    with pytest.raises(module.BackupError, match="leases"):
        module.backup(
            pg_store.database, tmp_path, tmp_path.parent / "backup", services_stopped=True
        )
    monkeypatch.setattr(module, "require_offline", lambda *args: None)
    monkeypatch.setattr(pg_store.database, "revision", lambda: "old")
    with pytest.raises(module.BackupError, match="Migrate"):
        module.backup(
            pg_store.database, tmp_path, tmp_path.parent / "backup", services_stopped=True
        )


@pytest.mark.parametrize("damage", ["change", "failure"])
def test_backup_detects_writes_or_pg_failure_and_removes_incomplete_copy(
    pg_database, tmp_path, monkeypatch, damage
):
    root, output = tmp_path / "data", tmp_path / "backup"
    root.mkdir()
    (root / "file").write_text("before")

    def run(*args):
        if damage == "failure":
            raise module.BackupError("controlled pg_dump failure")
        fake_dump(*args)
        (root / "file").write_text("after")

    monkeypatch.setattr(module, "run_pg", run)
    with pytest.raises(module.BackupError):
        module.backup(pg_database, root, output, services_stopped=True)
    assert not output.exists() and (root / "file").exists()


@pytest.mark.parametrize(
    "damage", ["file", "extra", "dump", "revision", "schema", "root", "json", "large"]
)
def test_restore_rejects_damaged_or_mismatched_backup_before_writing(
    saved_backup, pg_database, damage
):
    root, output = saved_backup
    for item in root.iterdir():
        item.unlink()
    root.rmdir()
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage in {"revision", "schema"}:
        manifest[damage] = "wrong"
    elif damage == "root":
        manifest["data_root"] = "/other/path"
    elif damage == "file":
        (output / "files" / "private.txt").write_text("tampered")
    elif damage == "extra":
        (output / "files" / "unexpected").write_text("extra")
    elif damage == "dump":
        (output / "postgres.dump").write_bytes(b"damaged")
    manifest_path.write_text(json.dumps(manifest))
    if damage == "json":
        manifest_path.write_text("[]")
    if damage == "large":
        with manifest_path.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(module.BackupError):
        module.restore(pg_database, root, output, services_stopped=True)
    assert not root.exists()


def test_restore_never_overwrites_existing_files_or_database(saved_backup, pg_database):
    root, output = saved_backup
    with pytest.raises(module.BackupError, match="Stop every"):
        module.restore(pg_database, root, output)
    with pytest.raises(module.BackupError, match="empty data"):
        module.restore(pg_database, root, output, services_stopped=True)
    (root / "private.txt").unlink()
    root.rmdir()
    with pytest.raises(module.BackupError, match="empty PostgreSQL"):
        module.restore(pg_database, root, output, services_stopped=True)
    assert not root.exists()


def test_inventory_and_copy_reject_symlinks_nonfiles_traversal_and_changes(tmp_path):
    root, target = tmp_path / "root", tmp_path / "target"
    root.mkdir()
    target.mkdir()
    file = root / "data"
    file.write_text("before")
    entries = module.inventory(root)
    file.write_text("after")
    with pytest.raises(module.BackupError, match="changed"):
        module.copy_files(root, target, entries)
    for name in ("../escape", "/absolute", "a/../b", "./file"):
        with pytest.raises(module.BackupError, match="path"):
            module.copy_files(root, target, {name: {}})
    fifo = root / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(module.BackupError, match="regular files"):
        module.file_digest(fifo)
    fifo.unlink()
    link = root / "link"
    link.symlink_to(file)
    with pytest.raises(ValueError):
        module.inventory(root)
    with pytest.raises(module.BackupError, match="directory"):
        module.inventory(tmp_path / "missing")
    directory_link = tmp_path / "linked"
    directory_link.symlink_to(root)
    with pytest.raises(module.BackupError, match="directory"):
        module.inventory(directory_link)


@pytest.mark.parametrize(
    "failure", [None, FileNotFoundError(), subprocess.CalledProcessError(1, "pg_dump")]
)
def test_pg_utilities_keep_credentials_out_of_arguments_and_redact_errors(monkeypatch, failure):
    runner = Mock(side_effect=failure)
    monkeypatch.setattr(module.subprocess, "run", runner)
    url = SimpleNamespace(
        host="db",
        port=5432,
        username="user",
        password="PRIVATE_KEY",
        database="target",
        query={"sslmode": "verify-full"},
    )
    database = SimpleNamespace(engine=SimpleNamespace(url=url))
    if failure:
        with pytest.raises(module.BackupError, match="did not complete") as error:
            module.run_pg(database, "pg_dump", ["--format=custom"])
        assert "PRIVATE_KEY" not in str(error.value)
    else:
        module.run_pg(database, "pg_dump", ["--format=custom"])
    args, kwargs = runner.call_args
    assert "PRIVATE_KEY" not in str(args)
    assert kwargs["env"]["PGPASSWORD"] == "PRIVATE_KEY"
    assert kwargs["env"]["PGSSLMODE"] == "verify-full"


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_backup_cli_closes_database_and_reports_only_counts(monkeypatch, tmp_path, capsys, action):
    database = SimpleNamespace(close=Mock())
    operation = Mock(return_value={"files": 2, "revision": REVISION})
    monkeypatch.setattr(module.Database, "from_env", lambda: database)
    monkeypatch.setattr(module, action, operation)
    monkeypatch.setattr(sys, "argv", ["backup", action, str(tmp_path), "--services-stopped"])
    module.main()
    assert json.loads(capsys.readouterr().out) == {"files": 2, "revision": REVISION}
    assert operation.call_args.kwargs["services_stopped"]
    database.close.assert_called_once()
