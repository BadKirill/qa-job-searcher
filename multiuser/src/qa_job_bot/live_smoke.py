import argparse
import asyncio
import io
import json
import os
import secrets
import tempfile
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx
from docx import Document
from pypdf import PdfReader
from sqlalchemy import select, text

from qa_job_bot import pg_schema as s
from qa_job_bot.agent_gateway import AgentGateway
from qa_job_bot.agent_result import response_metadata
from qa_job_bot.document_policy import validate_draft, validate_review
from qa_job_bot.models import Job
from qa_job_bot.openai_config import OpenAISettings
from qa_job_bot.operations import diagnostics
from qa_job_bot.pg_database import Database
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.runtime_worker import RuntimeWorker
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings, password_hash


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def gate_failure(status, error):
    message = (error or "").casefold()
    for fragment, category in (
        ("unsupported", "unsupported_fact"),
        ("not supported", "unsupported_fact"),
        ("cv title", "repeated_title"),
        ("document title", "candidate_identity"),
        ("invalid document", "document_schema"),
        ("evidence", "evidence_references"),
        ("оформления", "visual_review"),
    ):
        if fragment in message:
            return category
    return status


def synthetic_cv(name, index):
    role = "Backend Engineer" if index == 0 else "Product Designer"
    document = Document()
    document.add_heading(name, 0)
    document.add_paragraph(f"Belgrade, Serbia | smoke-{index}@example.test")
    document.add_paragraph(role)
    document.add_heading("Summary", 1)
    document.add_paragraph(
        "Backend engineer building Python APIs and PostgreSQL services."
        if index == 0
        else "Product designer creating accessible web interfaces in Figma."
    )
    document.add_heading("Skills", 1)
    document.add_paragraph(
        "Python, PostgreSQL, REST APIs, pytest"
        if index == 0
        else "Figma, interaction design, accessibility, usability research"
    )
    document.add_heading("Experience", 1)
    document.add_paragraph(f"Example Studio | {role} | 2021–2025")
    for item in (
        [
            "Developed Python REST APIs backed by PostgreSQL.",
            "Wrote API and integration tests with pytest.",
        ]
        if index == 0
        else [
            "Designed accessible web application interfaces in Figma.",
            "Conducted usability research and iterated interaction prototypes.",
        ]
    ):
        document.add_paragraph(item, style="List Bullet")
    document.add_heading("Education", 1)
    document.add_paragraph(
        "Example University | Computer Science | 2017–2021"
        if index == 0
        else "Example University | Interaction Design | 2017–2021"
    )
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


async def exercise(database, directory, gateway):
    password, stores, vacancies, identifiers = secrets.token_urlsafe(24), [], [], []
    names = ("Synthetic Alpha", "Synthetic Beta")
    for index, name in enumerate(names):
        user = database.create_user(f"smoke-{index}", name)
        with database.transaction() as connection:
            connection.execute(
                s.users.update()
                .where(s.users.c.id == user["id"])
                .values(password_hash=password_hash(password))
            )
        store = PostgresStore(database, user["id"])
        store.initialize()
        job = Job(
            "Synthetic",
            str(index),
            "Backend Engineer" if index == 0 else "Product Designer",
            "Synthetic Company",
            "Worldwide",
            f"https://example.test/smoke/{index}",
            description="Build Python APIs backed by PostgreSQL and test them with pytest."
            if index == 0
            else "Design accessible web interfaces using Figma and usability research.",
            score=80,
            remote=True,
        )
        stores.append(store)
        vacancies.append(job)
    settings = WebSettings(directory, ("http://smoke.test",))
    app = create_app(settings, database=database)
    clients = [
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://smoke.test",
            headers={"origin": "http://smoke.test"},
        )
        for _ in names
    ]
    sessions, tasks = set(), []
    try:
        for index, client in enumerate(clients):
            login = await client.post(
                "/api/login", json={"login": f"smoke-{index}", "password": password}
            )
            require(login.status_code == 200, "Smoke login failed")
            client.headers["x-csrf-token"] = login.json()["csrf_token"]
            job = vacancies[index]
            added = await client.post(
                "/api/manual-jobs",
                json={
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "description": job.description,
                    "url": job.url,
                    "request_id": "smoke-manual",
                },
            )
            require(added.status_code == 201, "Manual smoke vacancy was not saved")
            identifiers.append(added.json()["id"])
            uploaded = await client.post(
                "/api/template-drafts",
                files={
                    "file": (
                        "synthetic.docx",
                        synthetic_cv(names[index], index),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            require(uploaded.status_code == 201, "Synthetic CV upload failed")
            activated = await client.post(
                f"/api/template-drafts/{uploaded.json()['id']}/activate",
                json={"expected_version": None, "confirmed": True},
            )
            require(activated.status_code == 200, "Synthetic CV activation failed")
            task = await client.post(
                f"/api/jobs/{identifiers[index]}/documents",
                json={
                    "kind": "cv",
                    "language": "en",
                    "request_id": "positive",
                },
            )
            require(task.status_code == 202, "Smoke document was not queued")
            tasks.append(task.json()["id"])
        require(
            clients[0].cookies.get("qa_session") != clients[1].cookies.get("qa_session"),
            "Smoke authentication sessions overlap",
        )
        worker = RuntimeWorker(database, settings, gateway=gateway)
        await asyncio.gather(worker.once("document"), worker.once("document"))
        for index, client in enumerate(clients):
            task = (await client.get(f"/api/tasks/{tasks[index]}")).json()
            require(task["status"] == "ready", "Live positive document did not pass all checks")
            require(len(task["artifacts"]) == 2, "Live document lacks DOCX or PDF")
            for call in task["result"]["calls"]:
                identifier = call["metadata"].get("session_id")
                require(
                    identifier and identifier not in sessions,
                    "Live agent sessions overlap or are missing",
                )
                sessions.add(identifier)
            for artifact in task["artifacts"]:
                response = await client.get(f"/api/artifacts/{artifact['id']}/download")
                require(
                    response.status_code == 200,
                    f"Verified smoke artifact is not downloadable (HTTP {response.status_code})",
                )
                foreign = await clients[1 - index].get(f"/api/artifacts/{artifact['id']}/download")
                require(foreign.status_code == 404, "Foreign smoke artifact was disclosed")
                if artifact["format"] == "pdf":
                    content = " ".join(
                        page.extract_text()
                        for page in PdfReader(io.BytesIO(response.content)).pages
                    )
                    require(
                        names[index] in content and names[1 - index] not in content,
                        "Smoke PDF does not preserve candidate isolation",
                    )
        store = stores[0]
        negative = store.enqueue_document(identifiers[0], "cv", "en", "negative")
        execution = "negative-smoke-" + uuid4().hex
        task = store.claim(execution, lease_seconds=600)
        require(task["id"] == negative["id"], "Negative smoke task was not claimed")
        context = task["payload"]["context"]
        context.update(
            task_id=task["id"], owner_id=store.user_id, execution_id=execution, kind="cv"
        )
        unsupported = validate_draft(
            {
                "title": names[0],
                "language": "en",
                "sections": [
                    {
                        "heading": "Experience",
                        "paragraphs": [
                            {
                                "text": names[0] + " leads nuclear propulsion research.",
                                "fact_ids": [context["template"]["facts"][0]["id"]],
                            }
                        ],
                    }
                ],
            },
            context,
        )
        reviewed = await worker.gateway.review(context, unsupported)
        verdict = validate_review(reviewed, unsupported, context)
        require(
            verdict["verdict"] in {"revise", "needs_input"} and verdict["issues"],
            "Live reviewer accepted unsupported experience",
        )
        session = response_metadata(reviewed).get("session_id")
        require(session and session not in sessions, "Negative reviewer reused a session")
        sessions.add(session)
        require(
            store.finish_task(task["id"], execution, "blocked", error="Synthetic negative smoke"),
            "Negative document was not blocked",
        )
        require(store.task(task["id"])["artifacts"] == [], "Negative document published artifacts")
        denied = await clients[0].get(f"/api/tasks/{task['id']}/document-preview")
        require(denied.status_code == 404, "Negative smoke draft was published")
        denied_file = await clients[0].get(f"/api/artifacts/{task['id']}/download")
        require(denied_file.status_code == 404, "Negative smoke file was downloadable")
        return {
            "users": 2,
            "positive_documents": 2,
            "negative_blocked": True,
            "distinct_agent_sessions": len(sessions),
            "mcp": "stdio",
        }
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


async def run(credentials_dir):
    credentials = OpenAISettings.load(credentials_dir)
    source = Database.from_env()
    database = Database(source.engine.url, "qa_smoke_" + uuid4().hex)
    source.close()
    result, migrated = None, False
    try:
        database.migrate()
        migrated = True
        with tempfile.TemporaryDirectory(prefix="job-live-smoke-") as directory:
            result = await exercise(
                database, Path(directory).resolve(), AgentGateway(settings=credentials)
            )
            return {**result, "provider": "live_openai"}
    finally:
        try:
            if migrated:
                save_evidence(database, result)
            with database.transaction() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{database.schema}" CASCADE'))
        finally:
            database.close()


def save_evidence(database, result):
    directory = Path(os.getenv("SMOKE_REPORT_DIR", ".runtime/live-smoke"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with database.transaction() as connection:
        failures = Counter(
            gate_failure(status, error)
            for status, error in connection.execute(
                select(s.tasks.c.status, s.tasks.c.error).where(
                    s.tasks.c.status.in_(["failed", "blocked", "needs_input"])
                )
            )
        )
        rows = [
            dict(row)
            for row in connection.execute(
                select(
                    s.usage_reservations.c.id,
                    s.usage_reservations.c.task_id,
                    s.usage_reservations.c.payload,
                )
            ).mappings()
        ]
    descriptor = os.open(
        directory / (database.schema + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        json.dump(
            {
                "passed": result is not None,
                "result": result,
                "runtime": diagnostics(database),
                "gate_failures": dict(failures),
                "usage_reservations": rows,
            },
            output,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(run(arguments.credentials_dir))))


if __name__ == "__main__":
    main()
