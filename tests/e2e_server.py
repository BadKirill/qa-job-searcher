import asyncio
import base64
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn

from qa_job_bot.agent_gateway import AgentUnavailable
from qa_job_bot.document_policy import content_hash
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import WebStore
from qa_job_bot.worker import Worker


class FixtureGateway:
    async def generate(self, context):
        if context["job"].get("title") == "Senior QA Engineer 04":
            await asyncio.sleep(2)
        if "Unavailable" in context["job"].get("title", ""):
            raise AgentUnavailable("Fixture provider unavailable")
        return {
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

    async def review(self, context, draft):
        blocked = "Blocked" in context["job"].get("title", "")
        return {
            "verdict": "revise" if blocked else "pass",
            "issues": ["Неподтверждённый опыт"] if blocked else [],
            "draft_hash": content_hash(draft),
            "checked_fact_ids": [fact["id"] for fact in context["template"]["facts"]],
        }

    async def visual_review(self, context, draft, rendered_reference):
        reference = context["template"]["reference"]
        for visual in [reference, rendered_reference]:
            assert visual["pages"]
            assert all(
                base64.b64decode(page["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
                for page in visual["pages"]
            )
        blocked = context["job"].get("title") == "Senior QA Engineer 06"
        return {
            "verdict": "revise" if blocked else "pass",
            "issues": ["Неверные отступы в сформированном CV"] if blocked else [],
            "template_sha256": reference["sha256"],
            "rendered_sha256": rendered_reference["sha256"],
            "draft_hash": content_hash(draft),
        }

    async def chat(self, context, messages):
        return "Я могу помочь с шаблоном. Изменения фактов подтвердите в форме эталона."


class FixtureSearch:
    def __init__(self, store):
        self.store = store

    async def refresh(self):
        jobs = seed_jobs(self.store)
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
        )
        for index in range(1, 22)
    ]
    store.upsert_jobs(jobs)
    store.publish_search(jobs, [])
    return jobs


def main():
    with tempfile.TemporaryDirectory(prefix="qa-web-e2e-") as temporary:
        settings = WebSettings(
            Path(temporary),
            ("http://127.0.0.1:8011",),
            Path(__file__).resolve().parents[1] / "frontend" / "dist",
        )
        store = WebStore(settings.data_dir / "jobs.sqlite3")
        store.initialize()
        seed_jobs(store)
        app = create_app(settings, store)
        app.state.security.setup_file.write_text("e2e-setup-token-for-tests")
        worker = Worker(store, settings, FixtureGateway(), FixtureSearch(store))
        stop = asyncio.Event()

        @asynccontextmanager
        async def lifespan(application):
            task = asyncio.create_task(worker.run(stop))
            yield
            stop.set()
            await task

        app.router.lifespan_context = lifespan
        uvicorn.run(app, host="127.0.0.1", port=8011, access_log=False)


if __name__ == "__main__":
    main()
