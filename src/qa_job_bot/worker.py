import asyncio
import hashlib
import logging
import signal
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from langdetect import DetectorFactory, LangDetectException, detect_langs

from qa_job_bot.agent_gateway import AgentGateway, AgentUnavailable
from qa_job_bot.config import Settings, load_boards, load_profile, load_telegram_channels
from qa_job_bot.document_files import DocumentFileError, parse_template, render_documents
from qa_job_bot.document_policy import (
    PolicyViolation,
    bind_visual_review,
    content_hash,
    template_visual_reference,
    validate_draft,
    validate_review,
)
from qa_job_bot.service import JobSearchService, build_sources
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import WebStore

logger = logging.getLogger(__name__)
DetectorFactory.seed = 0


def vacancy_language(job: dict) -> str:
    text = str(job.get("description", "")).strip()
    if len(text) < 40:
        raise PolicyViolation("Недостаточно текста для определения языка вакансии.")
    try:
        guesses = detect_langs(text[:20000])
    except LangDetectException as error:
        raise PolicyViolation("Не удалось определить язык вакансии.") from error
    if guesses[0].prob < 0.75:
        raise PolicyViolation("Язык вакансии неоднозначен. Нужен полный текст описания.")
    return guesses[0].lang


def build_search(store: WebStore) -> tuple[JobSearchService, int]:
    settings = Settings.from_env(require_token=False)
    service = JobSearchService(
        load_profile(settings.search_profile_path),
        build_sources(
            load_boards(settings.job_boards_path),
            load_telegram_channels(settings.telegram_channels_path),
        ),
        store,
        settings.http_timeout_seconds,
    )
    return service, settings.search_interval_minutes * 60


class Worker:
    def __init__(self, store: WebStore, settings: WebSettings, gateway=None, search=None):
        self.store = store
        self.settings = settings
        self.gateway = gateway or AgentGateway(data_dir=settings.data_dir)
        self.search = search
        self.id = str(uuid4())

    def transition(self, task: dict, status: str, **kwargs) -> bool:
        return self.store.finish_task(task["id"], self.id, status, **kwargs)

    async def once(self) -> bool:
        task = self.store.claim(self.id)
        if not task:
            return False
        try:
            async with asyncio.timeout(840):
                if task["kind"] == "search":
                    if self.search is None:
                        self.search, _ = build_search(self.store)
                    report = await self.search.refresh()
                    self.store.publish_search(report.jobs, report.errors)
                    self.transition(
                        task, "ready", result={"count": len(report.jobs), "errors": report.errors}
                    )
                elif task["kind"] == "chat":
                    await self.chat(task)
                else:
                    await self.document(task)
        except AgentUnavailable:
            self.transition(
                task, "failed",
                error="OpenAI API недоступен. Проверьте API-ключ, баланс и лимит проекта.",
            )
        except (PolicyViolation, DocumentFileError) as error:
            self.transition(task, "blocked", error=str(error))
        except TimeoutError:
            self.transition(task, "failed", error="Превышено время ожидания. Повторите задание.")
        except Exception as error:
            logger.warning("Task %s failed: %s", task["id"], type(error).__name__)
            self.transition(
                task, "failed", error="Не удалось выполнить задание. Повторите попытку."
            )
        return True

    async def chat(self, task: dict) -> None:
        payload = task["payload"]
        context = {**payload["context"], "task_id": task["id"], "kind": "chat", "language": "ru"}
        reply = await self.gateway.chat(context, payload["messages"])
        self.store.finish_chat(task["id"], self.id, reply)

    async def document(self, task: dict) -> None:
        context = {**task["payload"]["context"], "task_id": task["id"], "kind": task["kind"]}
        reference = context["template"].get("reference")
        if context["template"].get("style", {}).get("source_format") == "pdf" and not reference:
            raise PolicyViolation("Перечитайте PDF в профиле и подтвердите целые абзацы эталона.")
        context["language"] = "en" if task["kind"] == "cv" else vacancy_language(context["job"])
        directory = self.settings.data_dir / "artifacts" / task["id"]
        for attempt in range(3):
            try:
                draft = validate_draft(await self.gateway.generate(context), context)
            except PolicyViolation as error:
                if attempt == 2:
                    raise
                context["revision_feedback"] = [str(error)]
                continue
            generator_metadata = dict(getattr(self.gateway, "last_run_metadata", {}))
            context["revision_draft"] = draft
            if not self.transition(task, "verifying"):
                return
            review = validate_review(await self.gateway.review(context, draft), draft, context)
            reviewer_metadata = dict(getattr(self.gateway, "last_run_metadata", {}))
            if review["verdict"] == "needs_input":
                self.transition(task, "needs_input", error="; ".join(review["issues"]))
                return
            if review["verdict"] == "revise":
                if attempt == 2:
                    self.transition(task, "blocked", error="; ".join(review["issues"]))
                    return
                context["revision_feedback"] = review["issues"]
                continue
            if not self.transition(task, "rendering"):
                return
            files = await asyncio.to_thread(
                render_documents, draft, context["template"], directory, task["kind"]
            )
            file_hashes = {}
            for file in files:
                path = Path(file["path"]).resolve()
                if not path.is_relative_to(directory.resolve()):
                    raise DocumentFileError("Недопустимый путь документа.")
                file_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            pdf_path = next(Path(file["path"]) for file in files if file["format"] == "pdf")
            pdf_content = pdf_path.read_bytes()
            if hashlib.sha256(pdf_content).hexdigest() != file_hashes[str(pdf_path.resolve())]:
                raise DocumentFileError("Документ изменился после рендеринга.")
            rendered = await asyncio.to_thread(parse_template, pdf_content, "result.pdf")
            visual_review = bind_visual_review(
                await self.gateway.visual_review(context, draft, rendered["reference"]),
                draft,
                template_visual_reference(context["template"]),
                rendered["reference"],
            )
            visual_reviewer_metadata = dict(getattr(self.gateway, "last_run_metadata", {}))
            if visual_review["verdict"] == "revise":
                visual_error = "Проверка оформления не пройдена: " + "; ".join(
                    visual_review["issues"]
                )
                if attempt == 2:
                    self.transition(task, "blocked", error=visual_error)
                    return
                context["revision_feedback"] = [visual_error]
                continue
            if not self.transition(task, "rendering"):
                return
            for file in files:
                path = Path(file["path"]).resolve()
                if hashlib.sha256(path.read_bytes()).hexdigest() != file_hashes[str(path)]:
                    raise DocumentFileError("Документ изменился после проверки.")
            artifact_ids = []
            for file in files:
                path = Path(file["path"]).resolve()
                artifact = self.store.add_artifact(
                    task["id"],
                    {
                        "filename": file["filename"],
                        "format": file["format"],
                        "path": str(path),
                        "sha256": file_hashes[str(path)],
                        "draft_hash": content_hash(draft),
                    },
                )
                artifact_ids.append(artifact["id"])
            self.transition(
                task,
                "ready",
                result={
                    "verified": True,
                    "draft_hash": content_hash(draft),
                    "approved_draft": draft,
                    "review": review,
                    "artifact_ids": artifact_ids,
                    "template_version": context["template"]["version"],
                    "generator": generator_metadata,
                    "reviewer": reviewer_metadata,
                    "visual_review": visual_review,
                    "visual_reviewer": visual_reviewer_metadata,
                },
            )
            return

    async def run(self, stop: asyncio.Event, interval: int = 10800) -> None:
        while not stop.is_set():
            last = self.store.get_setting("scheduled_search_at", 0)
            if time.time() - last >= interval:
                self.store.enqueue(
                    "search", "search", {}, f"scheduled:{int(time.time() // interval)}"
                )
                self.store.set_setting("scheduled_search_at", time.time())
            if not await self.once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)


async def async_main() -> None:
    settings = WebSettings.from_env()
    store = WebStore(settings.data_dir / "jobs.sqlite3")
    store.initialize()
    search, interval = build_search(store)
    worker = Worker(store, settings, search=search)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await worker.run(stop, interval)


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
