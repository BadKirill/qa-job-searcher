import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import uvicorn
from baseline_fixtures import seed_baseline_jobs
from pg_fixtures import drop_test_schema, personal_store, postgres_database

from qa_job_bot import pg_schema as pg
from qa_job_bot.agent_gateway import AgentUnavailable
from qa_job_bot.document_assessment import assess_review
from qa_job_bot.document_policy import content_hash, template_visual_reference
from qa_job_bot.legacy_cli import bootstrap_owner
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.runtime_worker import RuntimeWorker
from qa_job_bot.search_pipeline import PersonalSearchPipeline
from qa_job_bot.service import JobSearchService
from qa_job_bot.source_catalog import build_personal_sources
from qa_job_bot.source_http import PinnedTransport, SourceClient
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings
from qa_job_bot.worker import Worker


class FixtureGateway:
    async def generate(self, context):
        if context["job"].get("title") == "Senior QA Engineer 04":
            await asyncio.sleep(2)
        if "Unavailable" in context["job"].get("title", ""):
            raise AgentUnavailable("Fixture provider unavailable")
        draft = {
            "title": "Curriculum Vitae" if context["kind"] == "cv" else "Application",
            "language": context["language"],
            "sections": [
                {
                    "heading": "Experience",
                    "paragraphs": [
                        {"text": fact["text"], "fact_ids": [fact["id"]]}
                        for fact in context["template"]["facts"]
                    ],
                }
            ],
        }
        if (
            context["kind"] == "cv"
            and context["template"].get("reference")
            and context["job"].get("title") == "Senior QA Engineer 05"
        ):
            header = [
                {"text": fact["text"], "fact_ids": [fact["id"]], "kind": "body"}
                for fact in context["template"]["facts"]
                if fact.get("kind") in {"contact", "role"}
            ]
            draft["sections"].insert(0, {"heading": "", "paragraphs": header})
        return draft

    async def review(self, context, draft):
        blocked = "Blocked" in context["job"].get("title", "")
        response = {
            "verdict": "revise" if blocked else "pass",
            "issues": ["Неподтверждённый опыт"] if blocked else [],
            "draft_hash": content_hash(draft),
            "checked_fact_ids": [fact["id"] for fact in context["template"]["facts"]],
        }
        poorly_tailored = context["job"].get("title") == "Senior QA Engineer 07"
        response["assessment"] = {
            name: {
                "passed": not poorly_tailored or name != "vacancy_alignment",
                "issues": ["CV не адаптирован к вакансии"]
                if poorly_tailored and name == "vacancy_alignment"
                else [],
            }
            for name in ("vacancy_alignment", "evidence_selection", "document_structure")
        }
        return assess_review(response)[0]

    async def visual_review(self, context, draft, rendered_reference):
        reference = context["template"].get("reference")
        for visual in [item for item in (reference, rendered_reference) if item]:
            assert visual["pages"]
            assert all(
                base64.b64decode(page["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
                for page in visual["pages"]
            )
        blocked = context["job"].get("title") == "Senior QA Engineer 06"
        return {
            "verdict": "revise" if blocked else "pass",
            "issues": ["Неверные отступы в сформированном CV"] if blocked else [],
            "template_sha256": template_visual_reference(context["template"])["sha256"],
            "rendered_sha256": rendered_reference["sha256"],
            "draft_hash": content_hash(draft),
        }

    async def chat(self, context, messages):
        return "Я могу помочь с шаблоном. Изменения фактов подтвердите в форме эталона."


class FixtureSearch:
    def __init__(self, store, seed=None):
        self.store = store
        self.seed = seed or seed_jobs

    async def refresh(self):
        jobs = self.seed(self.store)
        return FetchReport(jobs, [])


def seed_jobs(store):
    jobs = [
        Job(
            source="Synthetic test fixture",
            source_id=str(index),
            title=(
                "Blocked QA Engineer"
                if index == 1
                else "Unavailable QA Engineer"
                if index == 2
                else f"Senior QA Engineer {index:02}"
            ),
            company=f"Example Company {index:02}",
            location="Remote · Europe",
            url=f"https://example.com/jobs/{index}",
            score=100 - index,
            description="Responsibilities:\nDesign automated tests with Python.\n"
            "Requirements:\nEnglish B2. Manual testing and automation experience.\n"
            "We are looking for a quality engineer to improve our software products.",
            salary="€4,000–6,000 / month" if index % 2 else "",
            match_reasons=["QA role", "Remote Europe"],
        )
        for index in range(1, 22)
    ]
    store.upsert_jobs(jobs)
    store.publish_search(jobs, [])
    return jobs


def seed_foreign_objects(database, settings):
    other = personal_store(database, "synthetic-other")
    path = settings.data_dir / "artifacts" / "foreign-task" / "foreign.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-synthetic-foreign-file")
    artifact = {
        "id": "foreign-artifact",
        "task_id": "foreign-task",
        "format": "pdf",
        "filename": "foreign.pdf",
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "draft_hash": "a" * 64,
    }
    with database.transaction() as connection:
        connection.execute(
            pg.templates.insert().values(
                id="foreign-template",
                user_id=other.user_id,
                status="draft",
                payload={
                    "id": "foreign-template",
                    "status": "draft",
                    "filename": "Foreign CV.pdf",
                    "facts": [{"id": "foreign-fact", "text": "Foreign private fact"}],
                },
            )
        )
        connection.execute(
            pg.conversations.insert().values(
                id="foreign-conversation",
                user_id=other.user_id,
                scope="template:foreign-template",
                template_id="foreign-template",
            )
        )
        connection.execute(
            pg.tasks.insert().values(
                id="foreign-task",
                user_id=other.user_id,
                kind="cv",
                scope="foreign-document",
                request_id="foreign-document",
                status="ready",
                result={
                    "verified": True,
                    "artifact_ids": ["foreign-artifact"],
                    "draft_hash": "a" * 64,
                },
            )
        )
        connection.execute(
            pg.artifacts.insert().values(
                id="foreign-artifact",
                user_id=other.user_id,
                task_id="foreign-task",
                payload=artifact,
            )
        )
        connection.execute(
            pg.snapshots.insert().values(
                id="foreign-snapshot", user_id=other.user_id, job_ids=[], snapshot_at=time.time()
            )
        )
    assert other.template("foreign-template")["facts"]
    assert other.artifact("foreign-artifact")["sha256"] == artifact["sha256"]
    assert other.get_conversation("foreign-conversation")["template_id"] == "foreign-template"


class PersonalFixtureSearch:
    def __init__(self, store):
        self.store = store

    async def refresh(self):
        profile, rows = self.store.search_configuration()
        service = JobSearchService(
            profile, build_personal_sources(rows, profile), self.store, 1, fixture_source_client
        )
        return await service.refresh()


fixture_source_counts = {}


def fixture_source_client(include_qa=False, **kwargs):
    async def resolver(host):
        return ["8.8.8.8"]

    async def response(request):
        host = request.headers["host"]
        token = request.url.path.rsplit("/", 1)[-1]
        if "lever.co" in host and token in {
            "stage5-history",
            "stage5-slow",
            "stage7-quota",
            "stage-design-layout",
        }:
            count = fixture_source_counts.get(token, 0) + 1
            fixture_source_counts[token] = count
            if token == "stage5-slow":
                await asyncio.sleep(1.5)
            if token == "stage5-history" and count == 2:
                return httpx.Response(503)
            if token == "stage5-history" and count == 3:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": str(index),
                        "text": f"Backend Engineer {index:02}",
                        "categories": {"location": "Worldwide"},
                        "workplaceType": "remote",
                        "hostedUrl": f"https://example.com/{token}/{index}",
                        "descriptionPlain": "Original Python requirements."
                        if count < 4
                        else "Changed requirements.",
                    }
                    for index in range(21 if token == "stage5-history" else 1)
                ],
            )
        if host == "remotive.com":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": role,
                            "title": role,
                            "company_name": "Example Company",
                            "candidate_required_location": "Europe",
                            "url": f"https://example.com/{index}",
                            "description": (
                                "We are looking for a professional to build "
                                "reliable software products."
                            ),
                        }
                        for index, role in enumerate(
                            ["Product Designer", "Backend Engineer"]
                            + (["Senior QA Engineer"] if include_qa else [])
                        )
                    ]
                },
            )
        if "lever.co" in host:
            if token == "working-languages":
                descriptions = {
                    "Russian post": "Ищем инженера для тестирования и развития нашего продукта.",
                    "English post": "We are hiring an engineer to improve our software products.",
                    "Russian team": "Help us build software. Working language: Russian.",
                    "English team": "Ищем инженера в команду. Рабочий язык — английский.",
                    "Bilingual team": "Working languages: English and Russian.",
                    "Unknown language": "Python, SQL",
                    "Russian company": "Our company was founded in Russia. We build software.",
                    "English at Russian company": (
                        "We are a Russian technology company. Working language: English."
                    ),
                    "Russian customers": "We build products for customers in Russia and Europe.",
                }
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": str(index),
                            "text": f"Backend Engineer · {label}",
                            "categories": {"location": "Worldwide"},
                            "workplaceType": "remote",
                            "hostedUrl": f"https://example.com/{token}/{index}",
                            "descriptionPlain": description,
                        }
                        for index, (label, description) in enumerate(descriptions.items())
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "id": str(index),
                        "text": title,
                        "categories": {"location": location},
                        "workplaceType": mode,
                        "hostedUrl": f"https://example.com/lever/{index}",
                        "descriptionPlain": "Build reliable products with Python and Figma.",
                    }
                    for index, (title, location, mode) in enumerate(
                        [
                            ("Junior Backend Engineer", "Berlin, Germany", "hybrid"),
                            ("Product Designer", "Berlin, Germany", "on-site"),
                            ("QA Engineer", "Belgrade, Serbia", "hybrid"),
                        ]
                    )
                ],
            )
        if host == "boards-api.greenhouse.io" and "stage-search-fix" in request.url.path:
            assert request.url.params.get("content") == "true"
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": index,
                            "title": f"Backend Engineer board {index:03}",
                            "location": {"name": "Remote Worldwide"},
                            "content": "<p>Fully remote software development with Python.</p>",
                            "absolute_url": f"https://example.com/search-fix/{index}",
                        }
                        for index in range(300)
                    ]
                },
            )
        if host == "t.me" and request.url.path.endswith("/partial_jobs"):
            if request.url.params.get("before"):
                return httpx.Response(429)
            return httpx.Response(
                200,
                text=(
                    '<div class="tgme_widget_message_wrap"><div data-post="partial_jobs/42">'
                    '<div class="tgme_widget_message_text">Backend Engineer Telegram<br>'
                    'Remote Worldwide</div></div></div><div class="tgme_channel_info"></div>'
                ),
            )
        if host == "t.me":
            return httpx.Response(200, text='<div class="tgme_channel_info"></div>')
        if host == "weworkremotely.com":
            return httpx.Response(200, text="<rss><channel/></rss>")
        payload = (
            {"data": {"jobs": []}}
            if host == "talent-api.remote.com"
            else ({"data": []} if host == "api.hirify.me" else {"jobs": []})
        )
        return httpx.Response(200, json=payload)

    return SourceClient(
        transport=PinnedTransport(resolver, lambda: httpx.MockTransport(response)), **kwargs
    )


def main():
    cutover = "--cutover" in sys.argv
    baseline = "--baseline" in sys.argv
    importing = "--legacy-import" in sys.argv or cutover
    account_mode = "--accounts" in sys.argv or cutover
    port = (
        8015
        if cutover
        else 8014
        if account_mode
        else 8013
        if importing
        else 8012
        if baseline
        else 8011
    )
    seed = seed_baseline_jobs if baseline else seed_jobs
    with (
        tempfile.TemporaryDirectory(prefix="qa-web-e2e-") as temporary,
        postgres_database() as database,
    ):
        temporary = str(Path(temporary).resolve())
        settings = WebSettings(
            Path(temporary) / "restored" if importing else Path(temporary),
            (f"http://127.0.0.1:{port}",),
            Path(__file__).resolve().parents[1] / "frontend" / "dist",
        )
        if account_mode:
            initial = bootstrap_owner(database, "owner" if cutover else "admin", "Administrator")
            store = PostgresStore(database, initial["user_id"])
        else:
            store = personal_store(database)
        if importing:
            from legacy_fixtures import seed_legacy

            from qa_job_bot.legacy_import import restore_snapshot

            legacy = seed_legacy(Path(temporary) / "source")
            restore_snapshot(
                legacy["snapshot"]["root"],
                database,
                store.user_id,
                settings.data_dir,
                telegram_chats=[101],
            )
        control_file = Path(__file__).resolve().parents[1] / ".runtime" / "e2e-cutover.json"
        if cutover:
            from qa_job_bot.cutover import Cutover
            from qa_job_bot.maintenance import set_mode
            from qa_job_bot.pilot import Pilot

            set_mode(settings.data_dir, True)
            Pilot(database).begin(store.user_id)
        if not account_mode:
            seed_foreign_objects(database, settings)
        if not importing:
            seed(store)
        app = (
            create_app(settings, database=database) if account_mode else create_app(settings, store)
        )
        client_factory = partial(fixture_source_client, include_qa=cutover)
        app.state.source_client_factory = client_factory
        if cutover:
            Cutover(database, settings.data_dir).record(services_stopped=True)
            with sqlite3.connect(
                Path(legacy["store"].path).resolve().as_uri() + "?mode=ro", uri=True
            ) as source:
                logical_hash = hashlib.sha256("\n".join(source.iterdump()).encode()).hexdigest()
            control_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                control_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                json.dump(
                    {
                        "schema": database.schema,
                        "data_dir": str(settings.data_dir),
                        "owner_id": store.user_id,
                        "source_hash": legacy["snapshot"]["manifest"]["source_hash"],
                        "source_database": str(legacy["store"].path),
                        "source_logical_hash": logical_hash,
                        "owner_conversation_id": legacy["conversation"]["id"],
                    },
                    output,
                )
        else:
            app.state.security.setup_file.write_text("e2e-setup-token-for-tests")
        worker = Worker(store, settings, FixtureGateway(), FixtureSearch(store, seed))
        stop = asyncio.Event()

        async def serve_accounts():
            from sqlalchemy import select

            seeded = cutover
            dispatcher = RuntimeWorker(
                database,
                settings,
                FixtureGateway(),
                PersonalSearchPipeline(client_factory, 1),
                heartbeat_seconds=0.05,
                lease_seconds=5,
            )
            while not stop.is_set():
                with database.transaction() as connection:
                    users = (
                        connection.execute(
                            select(pg.users).where(
                                pg.users.c.disabled.is_(False),
                                pg.users.c.password_hash.is_not(None),
                            )
                        )
                        .mappings()
                        .all()
                    )
                if users and not seeded:
                    expired = database.create_user("expired-invite", "Expired invitation")
                    with database.transaction() as connection:
                        connection.execute(
                            pg.invitations.insert().values(
                                id="expired-invitation",
                                user_id=expired["id"],
                                token_hash=hashlib.sha256(b"expired-synthetic-token").hexdigest(),
                                expires_at=time.time() - 1,
                            )
                        )
                    seeded = True
                await asyncio.gather(
                    *(
                        dispatcher.once(lane)
                        for lane in ["search", "chat", "document", "search", "chat", "document"]
                    )
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.05)

        @asynccontextmanager
        async def lifespan(application):
            task = (
                None
                if importing and not cutover
                else asyncio.create_task(serve_accounts() if account_mode else worker.run(stop))
            )
            try:
                yield
            finally:
                stop.set()
                try:
                    if task is not None:
                        await task
                finally:
                    drop_test_schema(database)
                    if cutover:
                        control_file.unlink(missing_ok=True)

        app.router.lifespan_context = lifespan
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
