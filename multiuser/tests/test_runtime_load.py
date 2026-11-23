import asyncio
import io
import json
import multiprocessing
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from e2e_server import FixtureGateway
from pypdf import PdfReader
from sqlalchemy import func, select
from test_accounts import PASSWORD, member, setup
from test_personal_documents import template

from qa_job_bot import pg_schema as s
from qa_job_bot.accounts import Accounts
from qa_job_bot.legacy_cli import bootstrap_owner
from qa_job_bot.models import Job, utcnow
from qa_job_bot.pg_database import Database
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.runtime_worker import RuntimeWorker
from qa_job_bot.search_pipeline import SourceBatch
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings


class SyntheticPipeline:
    def __init__(self, delay):
        self.delay = delay

    async def fetch(self, inputs):
        await asyncio.sleep(self.delay)
        source = inputs["sources"][0]
        job = Job(
            "Greenhouse",
            source["config"]["token"],
            inputs["profile"]["role_keywords"][0],
            source["config"]["token"],
            "Worldwide",
            "https://example.test/" + source["id"],
            description="Synthetic vacancy for operational rehearsal.",
            remote=True,
            workplace_type="remote",
        )
        return [SourceBatch(source, [job], utcnow().isoformat())]


def executor_process(url, schema, directory, delay):
    async def run():
        database = Database(url, schema)
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        try:
            worker = RuntimeWorker(
                database,
                WebSettings(Path(directory)),
                gateway=FixtureGateway(),
                search_pipeline=SyntheticPipeline(delay),
                heartbeat_seconds=0.1,
                lease_seconds=1 if delay == 30 else 90,
            )
            await worker.run(stop)
        finally:
            database.close()

    asyncio.run(run())


@contextmanager
def http_server(database, directory):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(sock.getsockname()[1])
    settings = WebSettings(directory, (origin,))
    app = create_app(settings, database=database)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started
        yield origin
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def wait_for(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Operational rehearsal timed out")


def test_twenty_http_sessions_survive_executor_restart_without_crossing_workspaces(
    pg_database, tmp_path
):
    started = time.monotonic()
    bootstrap_owner(pg_database, "admin", "Synthetic Administrator")
    accounts = Accounts(pg_database, WebSettings(tmp_path))
    admin, _ = setup(accounts)
    users = [member(accounts, admin, f"load-{index:02}")[0] for index in range(20)]
    for index, user in enumerate(users):
        store = PostgresStore(pg_database, user.id)
        template(store, f"Synthetic Candidate {index:02} develops confirmed software.")
    clients, timings, processes = [], [], []
    context = multiprocessing.get_context("spawn")

    def start_executor(delay):
        process = context.Process(
            target=executor_process,
            args=(
                pg_database.engine.url.render_as_string(hide_password=False),
                pg_database.schema,
                str(tmp_path),
                delay,
            ),
        )
        processes.append(process)
        process.start()
        return process

    def count(status):
        with pg_database.transaction() as connection:
            return connection.execute(
                select(func.count()).select_from(s.tasks).where(s.tasks.c.status == status)
            ).scalar_one()

    with http_server(pg_database, tmp_path) as origin, ThreadPoolExecutor(max_workers=20) as pool:

        def prepare(index):
            client = httpx.Client(
                base_url=origin,
                trust_env=False,
                timeout=30,
                headers={"origin": origin},
                limits=httpx.Limits(keepalive_expiry=1),
            )
            clients.append(client)
            before = time.monotonic()
            login = client.post(
                "/api/login", json={"login": users[index].login, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            client.headers["x-csrf-token"] = login.json()["csrf_token"]
            role = "backend engineer" if index % 2 == 0 else "product designer"
            saved = client.put(
                "/api/search-profile",
                json={"expected_version": 0, "profile": {"role_keywords": [role]}},
            )
            assert saved.status_code == 200, saved.text
            source = client.post(
                "/api/sources",
                json={
                    "kind": "greenhouse",
                    "config": {
                        "token": f"synthetic-{index:02}",
                        "name": f"Synthetic {index:02}",
                    },
                },
            )
            assert source.status_code == 201, source.text
            response = client.post("/api/search-runs", json={"request_id": "same-request"})
            assert response.status_code == 202, response.text
            timings.append(time.monotonic() - before)
            return index, client, response.json()["id"]

        sessions = list(pool.map(prepare, range(20)))
        try:
            assert len({client.cookies.get("qa_session") for _, client, _ in sessions}) == 20
            interrupted = start_executor(30)
            wait_for(lambda: count("running") == 2)
            interrupted.kill()
            interrupted.join(10)
            assert interrupted.exitcode == -signal.SIGKILL
            restarted = start_executor(0.01)
            wait_for(lambda: count("ready") == 20)

            def work(session):
                index, client, run_id = session
                before = time.monotonic()
                jobs = client.get("/api/jobs").json()
                assert jobs["total"] == 1
                job = jobs["items"][0]
                assert job["company"] == f"synthetic-{index:02}"
                other = sessions[(index + 1) % 20]
                assert client.get(f"/api/search-runs/{other[2]}").status_code == 404
                assert client.get(f"/api/tasks/{other[2]}").status_code == 404
                conversation = client.post("/api/conversations", json={"job_id": job["id"]})
                assert conversation.status_code == 200, conversation.text
                chat = client.post(
                    f"/api/conversations/{conversation.json()['id']}/messages",
                    json={
                        "text": f"Private synthetic request {index:02}",
                        "request_id": "same-chat",
                    },
                )
                assert chat.status_code == 202, chat.text
                document = client.post(
                    f"/api/jobs/{job['id']}/documents",
                    json={
                        "kind": "cv",
                        "language": "en",
                        "request_id": "same-document",
                    },
                )
                assert document.status_code == 202, document.text
                timings.append(time.monotonic() - before)
                return document.json()["id"]

            documents = list(pool.map(work, sessions))
            wait_for(lambda: count("ready") == 60)

            def inspect(session):
                index, client, _ = session
                task = client.get(f"/api/tasks/{documents[index]}").json()
                for artifact in task["artifacts"]:
                    response = client.get(f"/api/artifacts/{artifact['id']}/download")
                    assert response.status_code == 200, response.text
                    if artifact["format"] == "pdf":
                        text_value = " ".join(
                            page.extract_text()
                            for page in PdfReader(io.BytesIO(response.content)).pages
                        )
                        assert f"Candidate {index:02}" in text_value
                        assert f"Candidate {(index + 1) % 20:02}" not in text_value
                    other = sessions[(index + 1) % 20][1]
                    assert other.get(f"/api/artifacts/{artifact['id']}/download").status_code == 404
                assert len(client.get("/api/tasks").json()["items"]) == 3
                assert client.get("/api/usage").json()["used_calls"] == 4

            list(pool.map(inspect, sessions))
            with pg_database.transaction() as connection:
                attempts = (
                    connection.execute(select(s.tasks.c.attempts).where(s.tasks.c.kind == "search"))
                    .scalars()
                    .all()
                )
                assert sorted(attempts) == [1] * 18 + [2] * 2
            restarted.terminate()
            restarted.join(10)
            assert restarted.exitcode == 0
            ordered = sorted(timings)
            report = {
                "sessions": 20,
                "ready_tasks": 60,
                "recovered_searches": 2,
                "http_batches": len(timings),
                "batch_p50_ms": round(ordered[len(ordered) // 2] * 1000, 2),
                "batch_p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
                "duration_seconds": round(time.monotonic() - started, 2),
                "provider": "controlled",
                "live_openai": False,
            }
            output = Path(os.getenv("QA_LOAD_REPORT", "/tmp/job-stage9-load.json"))
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join(10)
            for client in clients:
                client.close()
