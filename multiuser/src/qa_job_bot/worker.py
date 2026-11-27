import asyncio
import copy
import hashlib
import logging
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from langdetect import LangDetectException, detect_langs

from qa_job_bot.agent_gateway import AgentGateway, AgentUnavailable
from qa_job_bot.agent_result import response_metadata
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
from qa_job_bot.maintenance import active as maintenance_active
from qa_job_bot.openai_config import OpenAIConfigurationError, OpenAISettings
from qa_job_bot.personal_files import checked_path, owner_root, private_directory
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.posting_details import (
    PublicPostingSearch,
    check_result,
    fetch_posting,
    known_posting_link,
    remote_com_original,
    remote_com_posting_key,
    same_posting,
)
from qa_job_bot.runtime_limits import QuotaExceeded, RuntimeLimits
from qa_job_bot.search_pipeline import PersonalSearchPipeline
from qa_job_bot.service import JobSearchService, build_sources
from qa_job_bot.source_catalog import build_personal_sources
from qa_job_bot.source_http import SourceClient
from qa_job_bot.storage import close_store, open_store
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError, WebStore

logger = logging.getLogger(__name__)


def bound_metadata(response, context, role):
    metadata = response_metadata(response)
    expected = {
        "owner_id": context["owner_id"],
        "task_id": context["task_id"],
        "role": role,
        "template_version": str(
            context["template"].get("version") or context["template"].get("id", "")
        ),
    }
    if any(key in metadata and metadata[key] != value for key, value in expected.items()):
        raise PolicyViolation("Ответ относится к другому заданию или эталону.")
    return metadata


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
    if isinstance(store, PostgresStore):
        profile, rows = store.search_configuration()
        return JobSearchService(
            profile, build_personal_sources(rows, profile), store, settings.http_timeout_seconds
        ), settings.search_interval_minutes * 60
    service = JobSearchService(
        load_profile(settings.search_profile_path),
        build_sources(
            load_boards(settings.job_boards_path),
            load_telegram_channels(settings.telegram_channels_path),
            load_profile(settings.search_profile_path),
        ),
        store,
        settings.http_timeout_seconds,
    )
    return service, settings.search_interval_minutes * 60


class Worker:
    def __init__(
        self,
        store: WebStore,
        settings: WebSettings,
        gateway=None,
        search=None,
        search_pipeline=None,
    ):
        self.store = store
        self.settings = settings
        self.gateway = gateway or AgentGateway(data_dir=settings.data_dir)
        self.managed_search = search is None
        self.search_pipeline = search_pipeline
        self.search = search
        self.id = str(uuid4())

    def transition(self, task: dict, status: str, **kwargs) -> bool:
        try:
            return self.store.finish_task(task["id"], self.id, status, **kwargs)
        except StoreError as error:
            if error.status != 403:
                raise
            return False

    async def once(self) -> bool:
        if maintenance_active(self.settings.data_dir):
            return False
        task = self.store.claim(self.id)
        if not task:
            return False
        return await self.execute(task)

    async def execute(self, task):
        try:
            async with asyncio.timeout(840):
                if task["kind"] == "search" and task["payload"].get("search_inputs"):
                    pipeline = self.search_pipeline or PersonalSearchPipeline(
                        timeout=Settings.from_env(require_token=False).http_timeout_seconds
                    )
                    batches = await pipeline.fetch(task["payload"]["search_inputs"])
                    self.store.complete_search(task, self.id, batches)
                elif (
                    task["kind"] == "search"
                    and isinstance(self.store, PostgresStore)
                    and self.managed_search
                ):
                    self.transition(
                        task,
                        "failed",
                        error="Для старого задания не сохранены настройки. Запустите поиск заново.",
                    )
                elif task["kind"] == "search":
                    if self.search is None or (
                        self.managed_search and isinstance(self.store, PostgresStore)
                    ):
                        self.search, _ = build_search(self.store)
                    report = await self.search.refresh()
                    self.store.publish_search(report.jobs, report.errors)
                    self.transition(
                        task, "ready", result={"count": len(report.jobs), "errors": report.errors}
                    )
                elif task["kind"] == "posting_check":
                    await self.posting_check(task)
                elif task["kind"] == "chat":
                    await self.chat(task)
                else:
                    await self.document(task)
        except QuotaExceeded as error:
            self.transition(task, "blocked", error=str(error))
        except AgentUnavailable:
            self.transition(
                task,
                "failed",
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

    async def posting_check(self, task):
        payload = task["payload"]
        job = payload["posting_job"]
        factory = getattr(self.search_pipeline, "client_factory", None) or (
            lambda: SourceClient(max_requests=3, max_response=4 * 1024 * 1024)
        )
        url = payload.get("original_url") or job.get("url") or ""
        direct = known_posting_link(url)
        remote_key = remote_com_posting_key(url)
        if not direct and remote_key:
            direct = await remote_com_original(remote_key, factory)
        result = await fetch_posting(direct.url if direct else url, factory)
        if (
            not payload["manual"]
            and not payload.get("automatic")
            and not direct
            and result.status == "unconfirmed"
        ):
            result = await self.find_original_posting(task, job, factory)
        self.store.complete_posting_check(task, self.id, result)

    async def find_original_posting(self, task, job, factory):
        try:
            settings = OpenAISettings.load(self.settings.data_dir)
        except OpenAIConfigurationError:
            return check_result("unconfirmed", "Поиск оригинала временно недоступен.", "")
        context = {"owner_id": task["user_id"], "task_id": task["id"], "execution_id": self.id}
        limits = RuntimeLimits(self.store.database)
        try:
            reservation = limits.reserve(context, "posting_search")
        except QuotaExceeded as error:
            return check_result("unconfirmed", str(error), "")
        if reservation is None:
            return check_result("unconfirmed", "Поиск оригинала занят. Повторите позже.", "")
        succeeded = False
        try:
            candidates = await PublicPostingSearch(settings).find(
                job, lambda event, data: limits.progress(reservation, event, data)
            )
            succeeded = bool(candidates)
            matches = {}
            for candidate in candidates:
                exact = await fetch_posting(candidate, factory)
                if exact.job and same_posting(job, exact.job):
                    matches[exact.url] = exact
            if len(matches) == 1:
                return next(iter(matches.values()))
        finally:
            limits.finish(reservation, succeeded)
        return check_result("unconfirmed", "Поиск не подтвердил конкретное объявление.", "")

    async def chat(self, task: dict) -> None:
        payload = task["payload"]
        context = self.task_context(task)
        context.update(kind="chat", language="ru")
        reply = await self.gateway.chat(copy.deepcopy(context), copy.deepcopy(payload["messages"]))
        if isinstance(self.store, PostgresStore):
            self.store.finish_chat(
                task["id"], self.id, str(reply), bound_metadata(reply, context, "chat")
            )
        else:
            self.store.finish_chat(task["id"], self.id, str(reply))

    def task_context(self, task):
        context = copy.deepcopy(task["payload"]["context"])
        owner = self.store.user_id if isinstance(self.store, PostgresStore) else "owner"
        if context.get("owner_id", owner) != owner:
            raise PolicyViolation("Контекст другого владельца недоступен.")
        context.update(owner_id=owner, task_id=task["id"], kind=task["kind"], execution_id=self.id)
        return context

    async def document(self, task: dict) -> None:
        context = self.task_context(task)
        reference = context["template"].get("reference")
        if context["template"].get("style", {}).get("source_format") == "pdf" and not reference:
            raise PolicyViolation("Перечитайте PDF в профиле и подтвердите целые абзацы эталона.")
        context["language"] = "en" if task["kind"] == "cv" else vacancy_language(context["job"])
        directory = (
            owner_root(self.settings, getattr(self.store, "user_id", None))
            / "artifacts"
            / task["id"]
        )
        checked_path(self.settings.data_dir, directory)
        if isinstance(self.store, PostgresStore):
            directory = checked_path(self.settings.data_dir, directory / f"attempt-{uuid4()}")
        calls = []
        for attempt in range(3):
            try:
                generated = await self.gateway.generate(copy.deepcopy(context))
                generator_metadata = bound_metadata(generated, context, "generator")
                calls.append(
                    {"role": "generator", "attempt": attempt, "metadata": generator_metadata}
                )
                draft = validate_draft(generated, context)
            except PolicyViolation as error:
                if attempt == 2:
                    raise
                context["revision_feedback"] = [str(error)]
                continue
            context["revision_draft"] = draft
            if not self.transition(task, "verifying"):
                return
            reviewed = await self.gateway.review(copy.deepcopy(context), copy.deepcopy(draft))
            reviewer_metadata = bound_metadata(reviewed, context, "reviewer")
            calls.append({"role": "reviewer", "attempt": attempt, "metadata": reviewer_metadata})
            review = validate_review(reviewed, draft, context)
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
            private_directory(
                self.settings, "artifacts", getattr(self.store, "user_id", None), task["id"]
            )
            directory.mkdir(exist_ok=True, mode=0o700)
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
            visual_result = await self.gateway.visual_review(
                copy.deepcopy(context), copy.deepcopy(draft), copy.deepcopy(rendered["reference"])
            )
            visual_reviewer_metadata = bound_metadata(visual_result, context, "reviewer")
            calls.append(
                {
                    "role": "visual_reviewer",
                    "attempt": attempt,
                    "metadata": visual_reviewer_metadata,
                }
            )
            visual_review = bind_visual_review(
                visual_result,
                draft,
                template_visual_reference(context["template"]),
                rendered["reference"],
            )
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
            result = {
                "verified": True,
                "draft_hash": content_hash(draft),
                "approved_draft": draft,
                "review": review,
                "template_version": context["template"]["version"],
                "generator": generator_metadata,
                "reviewer": reviewer_metadata,
                "visual_review": visual_review,
                "visual_reviewer": visual_reviewer_metadata,
                "calls": calls,
                "input_hash": content_hash(task["payload"]["context"]),
                "job_version": context.get("job_version"),
            }
            registrations = [
                {
                    "filename": file["filename"],
                    "format": file["format"],
                    "path": str(Path(file["path"]).resolve()),
                    "sha256": file_hashes[str(Path(file["path"]).resolve())],
                    "draft_hash": content_hash(draft),
                }
                for file in files
            ]
            if isinstance(self.store, PostgresStore):
                self.store.finish_document(task["id"], self.id, result, registrations)
            else:
                result["artifact_ids"] = [
                    self.store.add_artifact(task["id"], data)["id"] for data in registrations
                ]
                self.transition(task, "ready", result=result)
            return

    async def run(self, stop: asyncio.Event, interval: int = 10800) -> None:
        while not stop.is_set():
            if maintenance_active(self.settings.data_dir):
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
                continue
            last = self.store.get_setting("scheduled_search_at", 0)
            if time.time() - last >= interval:
                if isinstance(self.store, PostgresStore) and self.managed_search:
                    try:
                        self.store.enqueue_search(f"scheduled:{int(time.time() // interval)}")
                    except StoreError as error:
                        if error.status != 409:
                            raise
                else:
                    self.store.enqueue(
                        "search", "search", {}, f"scheduled:{int(time.time() // interval)}"
                    )
                self.store.set_setting("scheduled_search_at", time.time())
            if not await self.once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)


async def async_main() -> None:
    if os.getenv("DATABASE_URL"):
        from qa_job_bot.runtime_worker import async_main as shared_main

        await shared_main()
        return
    settings = WebSettings.from_env()
    store = open_store(settings)
    store.initialize()
    if isinstance(store, PostgresStore):
        search, interval = None, Settings.from_env(require_token=False).search_interval_minutes * 60
    else:
        search, interval = build_search(store)
    worker = Worker(store, settings, search=search)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(stop, interval)
    finally:
        close_store(store)


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
