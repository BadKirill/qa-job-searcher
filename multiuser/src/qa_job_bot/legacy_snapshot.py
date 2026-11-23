import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path, PurePosixPath

from qa_job_bot.models import utcnow
from qa_job_bot.web_store import digest

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 10000
CONFIG_FILES = ("search_profile.toml", "job_boards.toml", "telegram_channels.toml")
TABLE_COLUMNS = {
    "jobs": (
        "fingerprint source source_id title company location url published_at score "
        "first_seen_at last_seen_at payload"
    ),
    "subscriptions": "chat_id enabled created_at",
    "deliveries": "chat_id fingerprint delivered_at",
    "job_identities": "source_key fingerprint",
    "web_settings": "key value",
    "web_state": "job_id applied_at archived_at",
    "web_snapshots": "id job_ids created_at",
    "web_templates": "id status payload",
    "web_conversations": "id scope payload",
    "web_messages": "id conversation_id role text request_id created_at",
    "web_tasks": (
        "id kind scope request_id payload status result error created_at "
        "lease_until worker_id attempts"
    ),
    "web_task_requests": "request_id task_id payload",
    "web_artifacts": "id task_id payload",
    "web_sessions": "token_hash csrf expires_at",
    "web_login_attempts": "ip created_at",
    "schema_migrations": "version applied_at",
}


class ImportFailure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def decode_json(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        raise ImportFailure("invalid_json", "Повреждённые JSON-данные в снимке.") from None


def relative_path(value):
    if not isinstance(value, str):
        raise ImportFailure("unsafe_path", "Недопустимый путь в снимке.")
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ImportFailure("unsafe_path", "Недопустимый путь в снимке.")
    return path


def read_private_file(root, relative, limit=MAX_FILE_BYTES):
    path = relative_path(relative)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise ImportFailure("file_limit", "Файл не является допустимым обычным файлом.")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ImportFailure("file_limit", "Превышен размер файла снимка.")
            return data
    except OSError:
        raise ImportFailure(
            "unsafe_or_missing_file", "Файл отсутствует или путь небезопасен."
        ) from None
    finally:
        os.close(directory)


def write_private_file(root, relative, data):
    parts = relative_path(relative).parts
    parent = root
    for part in parts[:-1]:
        parent /= part
        parent.mkdir(exist_ok=True, mode=0o700)
    path = parent / parts[-1]
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_tables(connection):
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA trusted_schema=OFF")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ImportFailure("invalid_database", "Проверка целостности SQLite не пройдена.")
    entities = connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    result = {}
    for entity in entities:
        name = entity["name"]
        if (
            name not in TABLE_COLUMNS
            or entity["type"] != "table"
            or "VIRTUAL TABLE" in entity["sql"].upper()
        ):
            raise ImportFailure("unsupported_schema", "Неизвестная структура исходной базы.")
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
        allowed = set(TABLE_COLUMNS[name].split())
        required = allowed - {"payload"} if name == "jobs" else allowed
        if columns - allowed or not required <= columns:
            raise ImportFailure("unsupported_schema", "Столбцы исходной базы не поддерживаются.")
        result[name] = [
            dict(row)
            for row in connection.execute(
                f'SELECT rowid AS __legacy_order, * FROM "{name}" ORDER BY rowid'
            )
        ]
    if any(row["version"] not in {1, 2, 3} for row in result.get("schema_migrations", [])):
        raise ImportFailure("unsupported_schema", "Версия исходной базы не поддерживается.")
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError):
        raise ImportFailure("unsupported_value", "Недопустимые значения в исходной базе.") from None
    return result


def database_tables(data):
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.deserialize(data)
            return read_tables(connection)
    except sqlite3.Error:
        raise ImportFailure("invalid_database", "Снимок SQLite повреждён или недоступен.") from None


def backup_database(source, destination):
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_DATABASE_BYTES:
        raise ImportFailure("invalid_database", "Нужен обычный файл SQLite допустимого размера.")
    deadline = time.monotonic() + 30
    page_size = 4096

    def progress(status, remaining, total):
        if time.monotonic() > deadline or total * page_size > MAX_DATABASE_BYTES:
            raise ImportFailure("backup_timeout", "Не удалось получить ограниченный снимок SQLite.")

    try:
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as original:
            page_size = original.execute("PRAGMA page_size").fetchone()[0]
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy, pages=128, progress=progress, sleep=0.05)
                copy.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error:
        raise ImportFailure(
            "invalid_database", "Не удалось создать согласованную копию SQLite."
        ) from None
    destination.chmod(0o600)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())


def create_snapshot(database, data_dir, config_dir, output, legacy_data_root=None):
    database, data_dir, config_dir, output = map(Path, (database, data_dir, config_dir, output))
    if (
        output.exists()
        or data_dir.is_symlink()
        or config_dir.is_symlink()
        or not data_dir.is_dir()
        or not config_dir.is_dir()
        or output.resolve().is_relative_to(data_dir.resolve())
    ):
        raise ImportFailure(
            "snapshot_location", "Нужна новая директория снимка вне исходных данных."
        )
    output.mkdir(parents=True, mode=0o700)
    try:
        backup_database(database, output / "legacy.sqlite3")
        raw = read_private_file(output, "legacy.sqlite3", MAX_DATABASE_BYTES)
        tables = database_tables(raw)
        files = []
        total = 0
        for folder in ("templates", "artifacts"):
            directory = data_dir / folder
            if not directory.exists() and not directory.is_symlink():
                continue
            for current, directories, filenames in os.walk(directory, followlinks=False):
                if Path(current).is_symlink() or any(
                    (Path(current) / name).is_symlink() for name in directories
                ):
                    raise ImportFailure(
                        "unsafe_path", "Ссылки в приватных файлах не поддерживаются."
                    )
                for filename in sorted(filenames):
                    relative = (Path(current) / filename).relative_to(data_dir).as_posix()
                    content = read_private_file(data_dir, relative)
                    total += len(content)
                    if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                        raise ImportFailure("snapshot_limit", "Превышен объём приватных файлов.")
                    write_private_file(output, "data/" + relative, content)
                    files.append(
                        {"path": relative, "sha256": sha256(content), "size": len(content)}
                    )
        configs = []
        for name in CONFIG_FILES:
            content = read_private_file(config_dir, name, 1024 * 1024)
            write_private_file(output, "config/" + name, content)
            configs.append({"path": name, "sha256": sha256(content), "size": len(content)})
        manifest = {
            "format": 1,
            "database_sha256": sha256(raw),
            "tables_sha256": digest(tables),
            "files": sorted(files, key=lambda item: item["path"]),
            "config": configs,
            "legacy_data_root": str(legacy_data_root or data_dir.resolve()),
        }
        manifest["source_hash"] = snapshot_identity(manifest)
        manifest["created_at"] = utcnow().isoformat()
        write_private_file(
            output,
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
        )
        return load_snapshot(output)
    except Exception:
        import shutil

        shutil.rmtree(output)
        raise


def snapshot_identity(manifest):
    return digest(
        {key: manifest[key] for key in ("tables_sha256", "files", "config", "legacy_data_root")}
    )


def load_snapshot(root):
    root = Path(root)
    manifest = decode_json(read_private_file(root, "manifest.json", 4 * 1024 * 1024))
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise ImportFailure("snapshot_format", "Формат снимка не поддерживается.")
    try:
        raw = read_private_file(root, "legacy.sqlite3", MAX_DATABASE_BYTES)
        tables = database_tables(raw)
        if (
            sha256(raw) != manifest["database_sha256"]
            or digest(tables) != manifest["tables_sha256"]
            or snapshot_identity(manifest) != manifest["source_hash"]
        ):
            raise ImportFailure("snapshot_integrity", "Контрольные суммы снимка не совпадают.")
        seen, total, configuration = set(), 0, {}
        for category, prefix in (("files", "data/"), ("config", "config/")):
            for entry in manifest[category]:
                relative = relative_path(entry["path"])
                if category == "files" and relative.parts[0] not in {"templates", "artifacts"}:
                    raise ImportFailure("unsafe_path", "Недопустимая группа файлов снимка.")
                path = prefix + relative.as_posix()
                if path in seen:
                    raise ImportFailure("snapshot_integrity", "Повторный путь в манифесте.")
                seen.add(path)
                data = read_private_file(root, path)
                total += len(data)
                if sha256(data) != entry["sha256"] or len(data) != entry["size"]:
                    raise ImportFailure("snapshot_integrity", "Приватный файл снимка изменён.")
                if category == "config":
                    configuration[entry["path"]] = data
        if total > MAX_TOTAL_BYTES or len(seen) > MAX_FILES + len(CONFIG_FILES):
            raise ImportFailure("snapshot_limit", "Превышен объём снимка.")
        if {entry["path"] for entry in manifest["config"]} != set(CONFIG_FILES):
            raise ImportFailure("snapshot_integrity", "Снимок конфигурации неполон.")
    except (KeyError, TypeError, AttributeError):
        raise ImportFailure("snapshot_format", "Некорректный манифест снимка.") from None
    return {"root": root, "manifest": manifest, "tables": tables, "configuration": configuration}
