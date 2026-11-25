import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

if __package__:
    from .image_identity import fingerprint
else:
    from image_identity import fingerprint

ROOT = Path("/srv/qa-job-searcher")
BUCKET = "qa-job-searcher-577331852274-eu-north-1"
REGION = "eu-north-1"
LOCK = Path("/run/qa-job-searcher-backup.lock")
CADDY = Path("/etc/caddy/Caddyfile")
SERVICES = ["api", "scheduler", "search-worker", "chat-worker", "document-worker"]
FILES = {"runtime-image.tar.gz", "compose.runtime.yaml", "compose.aws.json", "Caddyfile"}


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def dc(*arguments, capture_output=False, **kwargs):
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
        **kwargs,
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
    if manifest["version"] != 2 or manifest["commit"] != commit or set(manifest["files"]) != FILES:
        raise ValueError("Release manifest scope mismatch")
    if not re.fullmatch("sha256:[0-9a-f]{64}", manifest["image_id"]):
        raise ValueError("Invalid image ID")
    validate_identifier(manifest["image_fingerprint"], 64)
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
    if fingerprint(image) != manifest["image_fingerprint"] or image["Architecture"] != "arm64":
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


def runtime_states(output):
    output = output.strip()
    rows = json.loads(output) if output.startswith("[") else map(json.loads, output.splitlines())
    states = {}
    by_service = {row["Service"]: row for row in rows}
    for service in ["postgres", "migrate", *SERVICES]:
        row = by_service.get(service, {})
        states[service] = {
            "state": row.get("State", "missing"),
            "health": row.get("Health"),
            "exit_code": row.get("ExitCode"),
        }
    return states


def wait_until_healthy(phase, compose, timeout=300):
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        states = runtime_states(compose("ps", "--all", "--format", "json"))
        if states != previous:
            print(json.dumps({"phase": phase, "runtime_health": states}), flush=True)
            previous = states
        migration = states["migrate"]
        if (
            migration["state"] == "exited"
            and migration["exit_code"] == 0
            and all(
                states[service]["state"] == "running" and states[service]["health"] == "healthy"
                for service in ["postgres", *SERVICES]
            )
        ):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Not all runtime services are healthy ({phase}, {timeout}s)")
        time.sleep(min(3, remaining))


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


def start_runtime(phase):
    print(json.dumps({"phase": phase, "event": "starting_runtime"}), flush=True)
    dc("up", "-d", "--no-build", "--wait-timeout", "180")
    wait_until_healthy(phase, lambda *args: dc(*args, capture_output=True, timeout=20).stdout)
    ready()
    run(["systemctl", "reload", "caddy"])


def replace(source, destination):
    temporary = destination.with_suffix(destination.suffix + ".deploying")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as content:
        shutil.copyfileobj(content, output)
    os.chmod(temporary, 0o600)
    if destination == CADDY:
        run(["chown", "root:caddy", str(temporary)])
        os.chmod(temporary, 0o640)
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
        start_runtime("activate")
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
        start_runtime("rollback")
        raise


def acquire_deploy_lock(lock, timeout=180):
    deadline = time.monotonic() + timeout
    waiting = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Backup/deploy lock is still busy after {timeout}s") from None
            if not waiting:
                print(json.dumps({"event": "waiting_for_backup_deploy_lock"}), flush=True)
                waiting = True
            time.sleep(min(3, remaining))
        else:
            if waiting:
                print(json.dumps({"event": "backup_deploy_lock_acquired"}), flush=True)
            return


def deploy(commit, expected):
    with LOCK.open("w") as lock:
        acquire_deploy_lock(lock)
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
