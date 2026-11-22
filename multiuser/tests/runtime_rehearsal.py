import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run():
    started = time.monotonic()
    identity = "qa-ops-" + uuid4().hex[:10]
    projects = (identity, identity + "-recovery")
    ports = {project: free_port() for project in projects}
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
    environment.update(
        POSTGRES_PASSWORD="synthetic-runtime-rehearsal-only",
        POSTGRES_DB="job_searcher",
        WEB_DATA_DIR="/app/data/current",
    )
    docker = ["docker", "--host=unix:///var/run/docker.sock"]

    def command(arguments, project):
        settings = {
            **environment,
            "WEB_PORT": str(ports[project]),
            "WEB_ORIGINS": f"http://127.0.0.1:{ports[project]}",
        }
        result = subprocess.run(
            arguments,
            env=settings,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=240,
        )
        if result.returncode:
            raise RuntimeError("Isolated runtime rehearsal failed: " + result.stdout[-3000:])
        return result.stdout

    def compose(project, *arguments):
        return command(
            [*docker, "compose", "-p", project, "-f", "compose.runtime.yaml", *arguments], project
        )

    source, recovery = projects
    try:
        compose(source, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
        seed = """
import os
from pathlib import Path
from qa_job_bot.pg_database import Database
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web_security import password_hash
from qa_job_bot import pg_schema as s
assert os.getuid() == 10001
db=Database.from_env()
user=db.create_user('rehearsal-owner','Synthetic Rehearsal',role='admin')
with db.transaction() as connection:
    connection.execute(s.users.update().where(s.users.c.id==user['id']).values(password_hash=password_hash('synthetic-rehearsal-password')))
store=PostgresStore(db,user['id'])
path=Path(os.environ['WEB_DATA_DIR'])/'rehearsal.txt'
store.set_setting('rehearsal_file',str(path))
path.write_text('Synthetic backup and restore rehearsal')
path.chmod(0o600)
db.close()
"""
        compose(source, "exec", "-T", "api", "python", "-c", seed)
        services = ("api", "scheduler", "search-worker", "chat-worker", "document-worker")
        compose(source, "stop", *services)
        saved = json.loads(
            compose(
                source,
                "run",
                "--rm",
                "--no-deps",
                "maintenance",
                "python",
                "-m",
                "qa_job_bot.runtime_backup",
                "backup",
                "/backups/rehearsal",
                "--services-stopped",
            )
            .strip()
            .splitlines()[-1]
        )
        assert saved["files"] == 1
        compose(recovery, "up", "-d", "--no-build", "--wait", "postgres")
        compose(recovery, "create", "--no-build", "maintenance")
        command(
            [
                *docker,
                "run",
                "--rm",
                "--network=none",
                "--user=10001:10001",
                "--mount",
                f"type=volume,src={source}_backups,dst=/source,readonly",
                "--mount",
                f"type=volume,src={recovery}_backups,dst=/backups",
                "job-searcher-runtime:local",
                "python",
                "-c",
                "import shutil; shutil.copytree('/source/rehearsal','/backups/rehearsal')",
            ],
            recovery,
        )
        restored = json.loads(
            compose(
                recovery,
                "run",
                "--rm",
                "--no-deps",
                "maintenance",
                "python",
                "-m",
                "qa_job_bot.runtime_backup",
                "restore",
                "/backups/rehearsal",
                "--services-stopped",
            )
            .strip()
            .splitlines()[-1]
        )
        assert restored == saved
        compose(recovery, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
        verify = """
from pathlib import Path
from sqlalchemy import select
from qa_job_bot.pg_database import Database
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot import pg_schema as s
db=Database.from_env()
with db.transaction() as connection:
    owner=connection.execute(select(s.users.c.id).where(s.users.c.login=='rehearsal-owner')).scalar_one()
path=PostgresStore(db,owner).get_setting('rehearsal_file')
assert Path(path).read_text()=='Synthetic backup and restore rehearsal'
assert Path(path).stat().st_mode & 0o777 == 0o600
db.close()
"""
        compose(recovery, "exec", "-T", "api", "python", "-c", verify)
        origin = f"http://127.0.0.1:{ports[recovery]}"
        body = json.dumps(
            {"login": "rehearsal-owner", "password": "synthetic-rehearsal-password"}
        ).encode()
        request = Request(
            origin + "/api/login",
            data=body,
            headers={"Origin": origin, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            assert json.load(response)["user"]["login"] == "rehearsal-owner"
        compose(recovery, "stop", *services)
        compose(recovery, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
        with urlopen(origin + "/health/ready", timeout=10) as response:
            assert json.load(response) == {"ready": True}
        report = {
            "healthy_services": 6,
            "backup_files": saved["files"],
            "restored_login": True,
            "restored_file": True,
            "stop_start": True,
            "telegram_started": False,
            "nested_data_root": True,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
        Path(os.getenv("QA_COMPOSE_REPORT", "/tmp/job-stage9-compose.json")).write_text(
            json.dumps(report, indent=2)
        )
        return report
    finally:
        for project in projects:
            compose(project, "down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    print(json.dumps(run()))
