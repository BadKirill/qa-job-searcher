import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path("/srv/qa-job-searcher")
BUCKET = "qa-job-searcher-577331852274-eu-north-1"
REGION = "eu-north-1"
LOCK = Path("/run/qa-job-searcher-backup.lock")
CADDY = Path("/etc/caddy/Caddyfile")
SERVICES = ["api", "scheduler", "search-worker", "chat-worker", "document-worker"]
FILES = {"runtime-image.tar.gz", "compose.runtime.yaml", "compose.aws.json", "Caddyfile"}


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def dc(*arguments, capture_output=False):
    return run(
        [
            "docker",
            "compose",
            "-p",
            "job-searcher-aws",
            "--env-file",
            str(ROOT / ".runtime/runtime.env"),
            "-f",
            str(ROOT / "compose.runtime.yaml"),
            "-f",
            str(ROOT / "compose.aws.json"),
            *arguments,
        ],
        capture_output=capture_output,
    )


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_identifier(value, length):
    if not re.fullmatch("[0-9a-f]{" + str(length) + "}", value):
        raise ValueError("Invalid release identifier")
    return value


def download(commit, name, directory):
    run(
        [
            "aws",
            "s3",
            "cp",
            f"s3://{BUCKET}/artifacts/releases/{commit}/{name}",
            str(directory / name),
            "--region",
            REGION,
            "--only-show-errors",
        ]
    )


def prepare(commit, expected):
    validate_identifier(commit, 40)
    validate_identifier(expected, 64)
    directory = ROOT / ".runtime/releases" / commit
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    download(commit, "release.json", directory)
    if file_hash(directory / "release.json") != expected:
        raise ValueError("Release manifest checksum mismatch")
    manifest = json.loads((directory / "release.json").read_text())
    if manifest["version"] != 1 or manifest["commit"] != commit or set(manifest["files"]) != FILES:
        raise ValueError("Release manifest scope mismatch")
    if not re.fullmatch("sha256:[0-9a-f]{64}", manifest["image_id"]):
        raise ValueError("Invalid image ID")
    for name, expected_hash in manifest["files"].items():
        validate_identifier(expected_hash, 64)
        download(commit, name, directory)
        if file_hash(directory / name) != expected_hash:
            raise ValueError("Release file checksum mismatch: " + name)
    run(["docker", "load", "-i", str(directory / "runtime-image.tar.gz")])
    image = json.loads(
        run(
            ["docker", "image", "inspect", "job-searcher-runtime:" + commit], capture_output=True
        ).stdout
    )[0]
    if image["Id"] != manifest["image_id"] or image["Architecture"] != "arm64":
        raise ValueError("Loaded image identity or architecture mismatch")
    if image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != commit:
        raise ValueError("Image source revision mismatch")
    run(["caddy", "validate", "--config", str(directory / "Caddyfile"), "--adapter", "caddyfile"])
    return directory


def revision():
    return dc(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "job_searcher",
        "-d",
        "job_searcher",
        "-Atc",
        "SELECT version_num FROM public.alembic_version",
        capture_output=True,
    ).stdout.strip()


def maintenance(enabled):
    dc(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "maintenance",
        "python",
        "-m",
        "qa_job_bot.maintenance",
        "on" if enabled else "off",
    )


def drain():
    script = """import time
from sqlalchemy import func, select
from qa_job_bot import pg_schema as schema
from qa_job_bot.operations import ACTIVE
from qa_job_bot.pg_database import Database
database = Database.from_env()
deadline = time.monotonic() + 180
try:
    while True:
        with database.transaction() as connection:
            query = select(func.count()).select_from(schema.tasks).where(
                schema.tasks.c.status.in_(ACTIVE), schema.tasks.c.lease_until > time.time()
            )
            active = connection.execute(query).scalar_one()
        if not active:
            break
        if time.monotonic() >= deadline:
            raise SystemExit('Active tasks did not drain')
        time.sleep(3)
finally:
    database.close()
"""
    dc("exec", "-T", "api", "python", "-c", script)


def healthy():
    rows = dc("ps", "--format", "json", capture_output=True).stdout
    states = {row["Service"]: row.get("Health") for row in map(json.loads, rows.splitlines())}
    if any(states.get(service) != "healthy" for service in ["postgres", *SERVICES]):
        raise RuntimeError("Not all runtime services are healthy")


def ready():
    origins = next(
        line.partition("=")[2]
        for line in (ROOT / ".runtime/runtime.env").read_text().splitlines()
        if line.startswith("WEB_ORIGINS=")
    )
    host = urllib.parse.urlparse(origins.split(",")[0]).netloc
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=20)
    try:
        connection.request("GET", "/health/ready", headers={"Host": host})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Production readiness failed")
    finally:
        connection.close()


def replace(source, destination):
    temporary = destination.with_suffix(destination.suffix + ".deploying")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o644 if destination == CADDY else 0o600)
    os.replace(temporary, destination)


def activate(directory):
    previous = ROOT / ".runtime/previous-release"
    previous.mkdir(mode=0o700, exist_ok=True)
    configuration = {
        "compose.runtime.yaml": ROOT / "compose.runtime.yaml",
        "compose.aws.json": ROOT / "compose.aws.json",
        "Caddyfile": CADDY,
    }
    for name, destination in configuration.items():
        shutil.copyfile(destination, previous / name)
    before = revision()
    switched = False
    maintenance(True)
    try:
        drain()
        dc("stop", *SERVICES)
        for name, destination in configuration.items():
            replace(directory / name, destination)
        switched = True
        dc("config", "--quiet")
        maintenance(False)
        dc("up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
        healthy()
        ready()
        run(["systemctl", "reload", "caddy"])
    except Exception:
        maintenance(True)
        if switched and revision() != before:
            dc("stop", *SERVICES)
            raise RuntimeError(
                "Schema changed; maintenance retained. Restore requires operator review."
            ) from None
        for name, destination in configuration.items():
            replace(previous / name, destination)
        maintenance(False)
        dc("up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
        run(["systemctl", "reload", "caddy"])
        raise


def deploy(commit, expected):
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = prepare(commit, expected)
        dc("config", "--quiet")
        run(
            ["/usr/local/sbin/qa-job-searcher-backup"],
            env={**os.environ, "QA_BACKUP_LOCK_HELD": "1"},
        )
        activate(directory)
        (ROOT / ".runtime/deployed-commit").write_text(commit + "\n")
        print(json.dumps({"deployed_commit": commit, "healthy": True}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("commit")
    parser.add_argument("manifest_sha256")
    arguments = parser.parse_args()
    deploy(arguments.commit, arguments.manifest_sha256)


if __name__ == "__main__":
    main()
