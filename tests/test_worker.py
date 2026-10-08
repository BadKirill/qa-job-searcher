import asyncio
import hashlib
import runpy
import signal
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from docx import Document
from pypdf import PdfReader

from qa_job_bot import worker as worker_module
from qa_job_bot.agent_gateway import AgentUnavailable
from qa_job_bot.document_files import DocumentFileError, parse_template
from qa_job_bot.document_policy import PolicyViolation, canonical_text, content_hash
from qa_job_bot.models import FetchReport, Job
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError, WebStore
from qa_job_bot.worker import Worker, build_search, vacancy_language

ENGLISH_JOB = (
    "We are looking for a software quality engineer to test our applications and APIs. "
    "The successful candidate will design test cases, automate checks "
    "and collaborate with developers."
)
RUSSIAN_JOB = (
    "Мы ищем инженера по тестированию программного обеспечения для проверки веб-приложений. "
    "В обязанности входит разработка тестовых сценариев, "
    "проверка требований и автоматизация тестов."
)


def sample_draft(language: str = "en") -> dict:
    return {
        "title": "Candidate CV",
        "language": language,
        "sections": [
            {
                "heading": "Experience",
                "paragraphs": [
                    {"text": "I test software applications and APIs.", "fact_ids": ["fact-1"]}
                ],
            }
        ],
    }


def review_result(draft: dict, verdict: str = "pass") -> dict:
    return {
        "verdict": verdict,
        "issues": [] if verdict == "pass" else ["Confirm the experience"],
        "draft_hash": content_hash(draft),
        "checked_fact_ids": ["fact-1"],
    }


@pytest.fixture
def worker(tmp_path: Path) -> Worker:
    settings = WebSettings(data_dir=tmp_path / "private")
    store = WebStore(settings.data_dir / "state.sqlite3")
    store.initialize()

    async def generate(context):
        return sample_draft(context["language"])

    gateway = SimpleNamespace(
        generate=AsyncMock(side_effect=generate),
        review=AsyncMock(side_effect=lambda context, draft: review_result(draft)),
        chat=AsyncMock(return_value="Подтвердите изменения в профиле."),
    )
    search = SimpleNamespace(refresh=AsyncMock(return_value=FetchReport([], [])))
    return Worker(store, settings, gateway=gateway, search=search)


def enqueue_document(worker: Worker, kind: str = "cv", description: str = ENGLISH_JOB) -> dict:
    context = {
        "owner_id": "owner",
        "job": {"description": description},
        "template": {
            "version": "version-1",
            "facts": [
                {
                    "id": "fact-1",
                    "confirmed": True,
                    "text": "I test software applications and APIs.",
                }
            ],
        },
    }
    return worker.store.enqueue(kind, "document", {"context": context}, "request")


def enqueue_pdf(worker, legacy=False):
    template = parse_template(
        (Path(__file__).parent / "fixtures/candidate.pdf").read_bytes(), "candidate.pdf"
    )
    template.update(
        {
            "version": "pdf-1",
            "facts": [
                {
                    "id": "fact-1",
                    "confirmed": True,
                    "text": "I test software applications and APIs.",
                }
            ],
        }
    )
    if legacy:
        template.pop("reference")
    context = {"owner_id": "owner", "job": {"description": ENGLISH_JOB}, "template": template}
    return worker.store.enqueue("cv", "pdf", {"context": context}, "pdf-request")


def visual_result(context, draft, rendered):
    return {
        "verdict": "pass",
        "issues": [],
        "draft_hash": content_hash(draft),
        "template_sha256": context["template"]["reference"]["sha256"],
        "rendered_sha256": rendered["sha256"],
    }


async def test_legacy_pdf_requires_reread_without_calling_agent(worker):
    task = enqueue_pdf(worker, legacy=True)
    await worker.once()
    result = worker.store.task(task["id"])
    assert result["status"] == "blocked" and "Перечитайте PDF" in result["error"]
    assert result["artifacts"] == []
    worker.gateway.generate.assert_not_awaited()


async def test_pdf_files_require_visual_review_of_actual_rendered_pages(worker):
    task = enqueue_pdf(worker)

    async def visual(context, draft, rendered):
        assert rendered["pages"][0]["image_base64"]
        assert context["template"]["reference"]["pages"][1]["image_base64"]
        worker.gateway.last_run_metadata = {"thread_id": "visual-session"}
        return visual_result(context, draft, rendered)

    worker.gateway.visual_review = AsyncMock(side_effect=visual)
    await worker.once()
    result = worker.store.task(task["id"])
    assert result["status"] == "ready", result["error"]
    worker.gateway.visual_review.assert_awaited_once()
    assert result["result"]["visual_reviewer"] == {"thread_id": "visual-session"}
    pdf = next(worker.store.artifact(a["id"]) for a in result["artifacts"] if a["format"] == "pdf")
    assert result["result"]["visual_review"]["rendered_sha256"] == pdf["sha256"]


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("reject", "blocked"),
        ("invalid", "blocked"),
        ("unavailable", "failed"),
        ("timeout", "failed"),
        ("cancel", "cancelled"),
    ],
)
async def test_visual_review_failure_and_cancellation_never_register_artifacts(
    worker, outcome, status
):
    task = enqueue_pdf(worker)

    async def visual(context, draft, rendered):
        if outcome == "unavailable":
            raise AgentUnavailable("private error")
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "invalid":
            return {"approved": True}
        if outcome == "cancel":
            worker.store.cancel_task(task["id"])
        result = visual_result(context, draft, rendered)
        if outcome == "reject":
            result.update(verdict="revise", issues=["Clipped content"])
        return result

    worker.gateway.visual_review = AsyncMock(side_effect=visual)
    await worker.once()
    result = worker.store.task(task["id"])
    assert result["status"] == status, result["error"]
    assert result["artifacts"] == [] and not result["result"]
    with worker.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM web_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("extension", ["pdf", "docx"])
async def test_files_changed_during_visual_review_never_register(worker, extension):
    task = enqueue_pdf(worker)

    async def visual(context, draft, rendered):
        path = worker.settings.data_dir / "artifacts" / task["id"] / ("document." + extension)
        path.write_bytes(path.read_bytes() + b"changed")
        return visual_result(context, draft, rendered)

    worker.gateway.visual_review = AsyncMock(side_effect=visual)
    await worker.once()
    result = worker.store.task(task["id"])
    assert result["status"] == "blocked" and "изменился" in result["error"]
    with worker.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM web_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("description,expected", [(ENGLISH_JOB, "en"), (RUSSIAN_JOB, "ru")])
def test_vacancy_language_uses_real_detection(description: str, expected: str) -> None:
    assert vacancy_language({"description": description}) == expected


@pytest.mark.parametrize("description", ["", "x" * 39])
def test_vacancy_language_rejects_insufficient_text(description: str) -> None:
    with pytest.raises(PolicyViolation, match="Недостаточно"):
        vacancy_language({"description": description})


def test_vacancy_language_rejects_text_without_language() -> None:
    with pytest.raises(PolicyViolation, match="Не удалось"):
        vacancy_language({"description": "0123456789" * 10})


def test_vacancy_language_confidence_boundary_and_input_limit(monkeypatch) -> None:
    seen = []
    probability = [0.749]

    def detect(text):
        seen.append(text)
        return [SimpleNamespace(prob=probability[0], lang="en")]

    monkeypatch.setattr(worker_module, "detect_langs", detect)
    with pytest.raises(PolicyViolation, match="неоднозначен"):
        vacancy_language({"description": "x" * 25000})
    probability[0] = 0.75
    assert vacancy_language({"description": "x" * 40}) == "en"
    assert len(seen[0]) == 20000


def test_build_search_uses_configuration_without_telegram_token(
    worker: Worker, monkeypatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SEARCH_INTERVAL_MINUTES", "7")
    monkeypatch.setenv("SEARCH_PROFILE_PATH", "/tmp/qa-missing-profile")
    monkeypatch.setenv("JOB_BOARDS_PATH", "/tmp/qa-missing-boards")
    monkeypatch.setenv("TELEGRAM_CHANNELS_PATH", "/tmp/qa-missing-channels")
    service, interval = build_search(worker.store)
    assert service.repository is worker.store
    assert len(service.sources) == 5
    assert interval == 420


async def test_empty_queue_has_no_agent_or_search_calls(worker: Worker) -> None:
    assert not await worker.once()
    worker.gateway.generate.assert_not_awaited()
    worker.search.refresh.assert_not_awaited()


@pytest.mark.parametrize("lazy", [False, True])
async def test_search_task_publishes_results_and_errors(worker: Worker, monkeypatch, lazy) -> None:
    job = Job("test", "1", "QA Engineer", "Acme", "Europe", "https://jobs.example/1")
    worker.store.upsert_jobs([job])
    search = worker.search
    search.refresh.return_value = FetchReport([job], ["Failed: ReadTimeout"])
    if lazy:
        worker.search = None
        monkeypatch.setattr(worker_module, "build_search", lambda store: (search, 10800))
    task = worker.store.enqueue("search", "search", {}, "search-request")
    assert await worker.once()
    result = worker.store.task(task["id"])
    assert result["status"] == "ready"
    assert result["result"] == {"count": 1, "errors": ["Failed: ReadTimeout"]}
    assert worker.store.get_setting("current_jobs") == [job.fingerprint]
    assert worker.search is search


@pytest.mark.parametrize("cancelled", [False, True])
async def test_chat_stores_only_live_task_reply(worker: Worker, cancelled: bool) -> None:
    template = worker.store.add_template({"facts": [{"id": "fact-1", "text": "API testing"}]})
    conversation = template["conversation_id"]
    task = worker.store.enqueue(
        "chat",
        "chat",
        {
            "context": {},
            "messages": [{"role": "user", "text": "Use English"}],
            "conversation_id": conversation,
        },
        "chat-request",
    )

    async def reply(context, messages):
        assert context["kind"] == "chat"
        assert context["language"] == "ru"
        assert context["task_id"] == task["id"]
        assert messages == [{"role": "user", "text": "Use English"}]
        if cancelled:
            worker.store.cancel_task(task["id"])
        return "Подтвердите изменения."

    worker.gateway.chat.side_effect = reply
    assert await worker.once()
    messages = worker.store.messages(conversation)
    assert len(messages) == (0 if cancelled else 1)
    if not cancelled:
        assert messages[0]["text"] == "Подтвердите изменения."
        assert messages[0]["request_id"] == "chat-request"
    assert worker.store.task(task["id"])["status"] == ("cancelled" if cancelled else "ready")


@pytest.mark.parametrize(
    "kind,description,language", [("cv", RUSSIAN_JOB, "en"), ("cover_letter", RUSSIAN_JOB, "ru")]
)
async def test_verified_documents_render_real_pdf_docx_and_bind_hashes(
    worker: Worker,
    kind: str,
    description: str,
    language: str,
) -> None:
    task = enqueue_document(worker, kind, description)
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == "ready", finished["error"]
    assert worker.gateway.generate.await_args.args[0]["language"] == language
    assert finished["result"]["verified"] is True
    assert finished["result"]["draft_hash"] == content_hash(sample_draft(language))
    assert finished["result"]["template_version"] == "version-1"
    assert {artifact["format"] for artifact in finished["artifacts"]} == {"pdf", "docx"}
    for summary in finished["artifacts"]:
        artifact = worker.store.artifact(summary["id"])
        path = Path(artifact["path"])
        assert path.is_relative_to(worker.settings.data_dir / "artifacts" / task["id"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
        assert artifact["draft_hash"] == finished["result"]["draft_hash"]
        if artifact["format"] == "pdf":
            text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
        else:
            text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
        assert " ".join(text.split()) == " ".join(canonical_text(sample_draft(language)).split())


@pytest.mark.parametrize(
    "verdict,status,attempts", [("needs_input", "needs_input", 1), ("revise", "blocked", 3)]
)
async def test_review_rejections_never_create_artifacts(
    worker: Worker, verdict, status, attempts
) -> None:
    task = enqueue_document(worker)
    worker.gateway.review.side_effect = lambda context, draft: review_result(draft, verdict)
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == status
    assert finished["error"] == "Confirm the experience"
    assert finished["artifacts"] == []
    assert worker.gateway.generate.await_count == attempts
    assert worker.gateway.review.await_count == attempts
    assert not (worker.settings.data_dir / "artifacts").exists()


async def test_revision_feedback_is_passed_and_success_is_fully_rechecked(worker: Worker) -> None:
    task = enqueue_document(worker)
    contexts = []

    async def generate(context):
        contexts.append(deepcopy(context))
        return sample_draft()

    worker.gateway.generate.side_effect = generate
    worker.gateway.review.side_effect = [
        review_result(sample_draft(), "revise"),
        review_result(sample_draft()),
    ]
    assert await worker.once()
    assert "revision_feedback" not in contexts[0]
    assert contexts[1]["revision_feedback"] == ["Confirm the experience"]
    assert worker.store.task(task["id"])["status"] == "ready"
    assert worker.gateway.review.await_count == 2


@pytest.mark.parametrize("stage", ["generate", "review"])
async def test_cancellation_during_agent_call_blocks_render_and_late_result(
    worker: Worker, stage
) -> None:
    task = enqueue_document(worker)

    async def cancel_generate(context):
        worker.store.cancel_task(task["id"])
        return sample_draft()

    async def cancel_review(context, draft):
        worker.store.cancel_task(task["id"])
        return review_result(draft)

    if stage == "generate":
        worker.gateway.generate.side_effect = cancel_generate
    else:
        worker.gateway.review.side_effect = cancel_review
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == "cancelled"
    assert finished["artifacts"] == []
    assert not (worker.settings.data_dir / "artifacts").exists()


@pytest.mark.parametrize(
    "error,status",
    [
        (AgentUnavailable("private error"), "failed"),
        (PolicyViolation("Unsupported experience"), "blocked"),
        (DocumentFileError("Invalid document"), "blocked"),
        (TimeoutError("private timeout"), "failed"),
        (RuntimeError("private credentials"), "failed"),
    ],
)
async def test_worker_records_safe_failures_without_ready_artifacts(
    worker: Worker, error, status, caplog
) -> None:
    task = enqueue_document(worker)
    worker.gateway.generate.side_effect = error
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == status
    assert finished["artifacts"] == []
    assert "private" not in finished["error"]
    assert "private" not in caplog.text


async def test_invalid_reviewer_response_blocks_document(worker: Worker) -> None:
    task = enqueue_document(worker)
    worker.gateway.review.side_effect = None
    worker.gateway.review.return_value = {"approved": True}
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == "blocked"
    assert finished["artifacts"] == []


async def test_renderer_path_escape_blocks_registration(
    worker: Worker, monkeypatch, tmp_path
) -> None:
    task = enqueue_document(worker)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private external file")
    monkeypatch.setattr(
        worker_module,
        "render_documents",
        lambda *args: [{"path": outside, "filename": "document.pdf", "format": "pdf"}],
    )
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == "blocked"
    assert finished["artifacts"] == []
    assert "Недопустимый путь" in finished["error"]


async def test_cancellation_while_rendering_prevents_download(worker: Worker, monkeypatch) -> None:
    task = enqueue_document(worker)
    original = worker_module.render_documents

    def render(draft, template, directory):
        files = original(draft, template, directory)
        worker.store.cancel_task(task["id"])
        return files

    monkeypatch.setattr(worker_module, "render_documents", render)
    assert await worker.once()
    finished = worker.store.task(task["id"])
    assert finished["status"] == "cancelled"
    for artifact in finished["artifacts"]:
        with pytest.raises(StoreError) as error:
            worker.store.artifact(artifact["id"])
        assert error.value.status == 409


async def test_scheduler_enqueues_once_and_respects_saved_interval(
    worker: Worker, monkeypatch
) -> None:
    monkeypatch.setattr(worker_module.time, "time", lambda: 50000)
    stop = asyncio.Event()
    iterations = 0

    async def once():
        nonlocal iterations
        iterations += 1
        if iterations == 2:
            stop.set()
            return False
        return True

    monkeypatch.setattr(worker, "once", once)
    await worker.run(stop, interval=10800)
    tasks = worker.store.tasks()
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "search"
    assert worker.store.get_setting("scheduled_search_at") == 50000
    assert iterations == 2


async def test_idle_scheduler_recovers_wait_timeout(worker: Worker, monkeypatch) -> None:
    stop = asyncio.Event()
    waits = 0
    worker.store.set_setting("scheduled_search_at", 50000)
    monkeypatch.setattr(worker_module.time, "time", lambda: 50000)

    async def wait_for(awaitable, timeout):
        nonlocal waits
        awaitable.close()
        waits += 1
        assert timeout == 1
        if waits == 2:
            stop.set()
        raise TimeoutError

    monkeypatch.setattr(worker_module.asyncio, "wait_for", wait_for)
    await worker.run(stop)
    assert waits == 2
    assert worker.store.tasks() == []


def test_default_gateway_is_constructed(worker: Worker, monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(worker_module, "AgentGateway", lambda data_dir: sentinel)
    assert Worker(worker.store, worker.settings).gateway is sentinel


async def test_worker_entrypoint_builds_store_and_registers_shutdown(
    worker: Worker, monkeypatch
) -> None:
    registrations = {}
    monkeypatch.setattr(worker_module.WebSettings, "from_env", lambda: worker.settings)
    monkeypatch.setattr(worker_module, "build_search", lambda store: (worker.search, 60))
    run = AsyncMock()
    monkeypatch.setattr(
        worker_module, "Worker", lambda store, settings, search: SimpleNamespace(run=run)
    )
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: registrations.update({sig: callback})
    )
    await worker_module.async_main()
    run.assert_awaited_once()
    assert run.await_args.args[1] == 60
    assert set(registrations) == {signal.SIGTERM, signal.SIGINT}
    registrations[signal.SIGTERM]()
    assert run.await_args.args[0].is_set()
    assert (worker.settings.data_dir / "jobs.sqlite3").exists()


def test_sync_worker_entrypoint_runs_async_main(monkeypatch) -> None:
    main = AsyncMock()
    monkeypatch.setattr(worker_module, "async_main", main)
    worker_module.main()
    main.assert_awaited_once()


def test_worker_entrypoint_handles_interrupt_and_module_dispatch(monkeypatch) -> None:
    called = []

    def run(coroutine):
        called.append(coroutine.cr_code.co_name)
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_module.asyncio, "run", run)
    worker_module.main()
    runpy.run_path(worker_module.__file__, run_name="__main__")
    assert called == ["async_main", "async_main"]
