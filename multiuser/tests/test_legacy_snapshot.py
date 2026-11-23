import json
import os
import sqlite3
from contextlib import closing

import pytest

from qa_job_bot import legacy_snapshot as snapshotting
from qa_job_bot.legacy_plan import Paths
from qa_job_bot.legacy_snapshot import (
    CONFIG_FILES,
    ImportFailure,
    create_snapshot,
    database_tables,
    decode_json,
    load_snapshot,
    read_private_file,
    read_tables,
    relative_path,
    snapshot_identity,
    write_private_file,
)
from qa_job_bot.web_store import WebStore


@pytest.fixture
def source(tmp_path):
    data, config = tmp_path / "data", tmp_path / "config"
    config.mkdir()
    for name in CONFIG_FILES:
        (config / name).write_text("" if name != "telegram_channels.toml" else "channels=[]")
    store = WebStore(data / "jobs.sqlite3")
    store.initialize()
    write_private_file(data, "templates/source.bin", b"synthetic-private-file")
    write_private_file(data, "artifacts/task/intermediate.bin", b"synthetic-intermediate")
    return store.path, data, config


def test_snapshot_is_private_complete_and_stable_without_mutating_source(source, tmp_path):
    database, data, config = source
    initial = database.read_bytes()
    first = create_snapshot(database, data, config, tmp_path / "snapshot")
    second = create_snapshot(database, data, config, tmp_path / "second")
    assert initial == database.read_bytes()
    assert first["manifest"]["source_hash"] == second["manifest"]["source_hash"]
    assert len(first["manifest"]["files"]) == 2
    assert set(first["configuration"]) == set(CONFIG_FILES)
    assert (first["root"].stat().st_mode & 0o777) == 0o700
    assert all(
        (path.stat().st_mode & 0o777) == 0o600
        for path in first["root"].rglob("*")
        if path.is_file()
    )
    assert load_snapshot(first["root"])["tables"] == first["tables"]
    assert (
        read_private_file(first["root"], "data/templates/source.bin") == b"synthetic-private-file"
    )


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077])
def test_snapshot_nested_directories_are_private_regardless_of_umask(source, tmp_path, mask):
    previous = os.umask(mask)
    try:
        snapshot = create_snapshot(*source, tmp_path / "private-snapshot")
    finally:
        os.umask(previous)
    assert all(path.stat().st_mode & 0o077 == 0 for path in snapshot["root"].rglob("*"))


@pytest.mark.parametrize(
    "value",
    [None, 1, "", ".", "..", "../a", "a/../b", "a//b", "a/./b", "/etc/passwd", "a\\b", "a\x00b"],
)
def test_relative_path_rejects_escaping_or_ambiguous_values(value):
    with pytest.raises(ImportFailure):
        relative_path(value)


def test_private_file_reader_rejects_symlinks_missing_fifo_and_limits(tmp_path):
    write_private_file(tmp_path, "safe/large", b"12345")
    (tmp_path / "link").symlink_to(tmp_path / "safe", target_is_directory=True)
    (tmp_path / "safe/file-link").symlink_to(tmp_path / "safe/large")
    os.mkfifo(tmp_path / "pipe")
    for name, limit in [
        ("link/large", 10),
        ("safe/file-link", 10),
        ("missing", 10),
        ("pipe", 10),
        ("safe/large", 4),
    ]:
        with pytest.raises(ImportFailure):
            read_private_file(tmp_path, name, limit)
    assert read_private_file(tmp_path, "safe/large", 5) == b"12345"


@pytest.mark.parametrize(
    "problem",
    [
        "existing",
        "nested",
        "data-link",
        "config-link",
        "missing-data",
        "db-link",
        "db-invalid",
        "file-link",
        "dir-link",
        "file-missing-config",
        "size-limit",
        "count-limit",
        "total-limit",
    ],
)
def test_snapshot_failure_cleans_only_new_output(source, tmp_path, monkeypatch, problem):
    database, data, config = source
    output = tmp_path / "snapshot"
    if problem == "existing":
        output.mkdir()
        (output / "keep").write_text("preserve")
    elif problem == "nested":
        output = data / "snapshot"
    elif problem in {"data-link", "config-link", "db-link"}:
        link = tmp_path / "link"
        link.symlink_to({"data-link": data, "config-link": config, "db-link": database}[problem])
        if problem == "data-link":
            data = link
        elif problem == "config-link":
            config = link
        else:
            database = link
    elif problem == "missing-data":
        data = tmp_path / "missing"
    elif problem == "db-invalid":
        database.write_bytes(b"not-a-database")
    elif problem in {"file-link", "dir-link"}:
        (data / "templates/link").symlink_to(
            config if problem == "dir-link" else database, target_is_directory=problem == "dir-link"
        )
    elif problem == "file-missing-config":
        (config / CONFIG_FILES[0]).unlink()
    elif problem == "size-limit":
        monkeypatch.setattr(snapshotting, "MAX_DATABASE_BYTES", 1)
    elif problem == "count-limit":
        monkeypatch.setattr(snapshotting, "MAX_FILES", 1)
    elif problem == "total-limit":
        monkeypatch.setattr(snapshotting, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(ImportFailure):
        create_snapshot(database, data, config, output)
    if problem == "existing":
        assert (output / "keep").read_text() == "preserve"
    else:
        assert not output.exists()
    assert data.exists() or problem == "missing-data"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE unknown(secret TEXT)",
        "CREATE VIEW jobs AS SELECT 1 AS fingerprint",
        "CREATE VIRTUAL TABLE jobs USING fts5(fingerprint)",
        "CREATE TABLE jobs(fingerprint TEXT)",
        "CREATE TABLE web_settings(key TEXT,value TEXT,secret TEXT)",
        (
            "CREATE TABLE schema_migrations(version INT,applied_at TEXT); "
            "INSERT INTO schema_migrations VALUES(99,'old')"
        ),
        (
            "CREATE TABLE web_settings(key TEXT,value BLOB); "
            "INSERT INTO web_settings VALUES('key',X'FF')"
        ),
    ],
)
def test_sqlite_schema_and_values_are_strictly_validated(sql):
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(sql)
        with pytest.raises(ImportFailure):
            read_tables(connection)


def test_corrupt_database_and_json_are_sanitized():
    with pytest.raises(ImportFailure) as error:
        database_tables(b"not a SQLite file")
    assert error.value.code == "invalid_database"
    for value in (None, b"\xff", "{bad-private-input"):
        with pytest.raises(ImportFailure) as error:
            decode_json(value)
        assert "private-input" not in str(error.value)


@pytest.mark.parametrize(
    "problem",
    [
        "format",
        "missing-key",
        "database",
        "table-hash",
        "file",
        "file-size",
        "duplicate",
        "group",
        "config",
        "limit",
    ],
)
def test_load_snapshot_detects_tampered_manifest_and_files(source, tmp_path, monkeypatch, problem):
    root = create_snapshot(*source, tmp_path / "snapshot")["root"]
    path = root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    if problem == "format":
        manifest["format"] = 99
    elif problem == "missing-key":
        del manifest["database_sha256"]
    elif problem == "database":
        (root / "legacy.sqlite3").write_bytes(b"bad SQLite")
    elif problem == "table-hash":
        manifest["tables_sha256"] = "wrong"
    elif problem == "file":
        (root / "data/templates/source.bin").write_bytes(b"changed")
    elif problem == "file-size":
        manifest["files"][0]["size"] += 1
    elif problem == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif problem == "group":
        manifest["files"][0]["path"] = "secrets/key"
    elif problem == "config":
        manifest["config"].pop()
    elif problem == "limit":
        monkeypatch.setattr(snapshotting, "MAX_TOTAL_BYTES", 1)
    if problem not in {"format", "missing-key"}:
        manifest["source_hash"] = snapshot_identity(manifest)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ImportFailure):
        load_snapshot(root)


def test_legacy_file_relocation_preserves_payload_and_rejects_outside_or_missing_files(
    source, tmp_path
):
    snapshot = create_snapshot(*source, tmp_path / "snapshot")
    paths = Paths(snapshot, tmp_path / "target")
    assert paths.relative(str(source[1] / "templates/source.bin")) == "templates/source.bin"
    assert paths.relative("data/templates/source.bin") == "templates/source.bin"
    assert paths.relocate({"facts": ["kept"]}, "templates") == {"facts": ["kept"]}
    for value in (None, "/etc/private", "../templates/source.bin"):
        with pytest.raises(ImportFailure):
            paths.relative(value)
    for value in ("templates/missing", "artifacts/task/intermediate.bin"):
        with pytest.raises(ImportFailure):
            paths.relocate({"path": value}, "templates")
    snapshot["manifest"]["legacy_data_root"] = "relative"
    with pytest.raises(ImportFailure):
        Paths(snapshot, tmp_path)
