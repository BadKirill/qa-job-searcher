import asyncio
import base64
import hashlib
import hmac
import os
import shutil
import time
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qa_job_bot.document_files import DocumentFileError, parse_template
from qa_job_bot.document_policy import content_hash
from qa_job_bot.web_security import Security, WebSettings
from qa_job_bot.web_store import StoreError, WebStore, digest, encode

COOKIE = "qa_session"
MAX_BODY = 11 * 1024 * 1024
Text = Annotated[str, Field(min_length=1, max_length=120, strict=True)]


def stage_task_directory(settings: WebSettings, task_id: str) -> tuple[Path, Path] | None:
    root = (settings.data_dir / "artifacts").resolve()
    source = root / task_id
    if source.is_symlink():
        raise StoreError("Каталог документа небезопасен для удаления.", 409)
    resolved = source.resolve()
    if resolved.parent != root:
        raise StoreError("Каталог документа небезопасен для удаления.", 409)
    if not resolved.exists():
        return None
    if not resolved.is_dir():
        raise StoreError("Каталог документа повреждён.", 409)
    staged = root / f".deleting-{task_id}-{uuid4()}"
    resolved.rename(staged)
    return resolved, staged


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Input):
    password: Annotated[str, Field(min_length=1, max_length=256)]


class Setup(Login):
    token: Annotated[str, Field(min_length=1, max_length=256)]
    password: Annotated[str, Field(min_length=12, max_length=256)]


class State(Input):
    applied: bool | None = None
    archived: bool | None = None


class Fact(Input):
    id: Text
    text: Annotated[str, Field(min_length=1, max_length=6000)]
    source: Annotated[str, Field(min_length=1, max_length=400)]
    kind: Literal["body", "bullet", "entry", "role", "contact", "heading", "title"] = "body"


class TemplateChange(Input):
    facts: Annotated[list[Fact], Field(min_length=1, max_length=500)]
    instructions: Annotated[str, Field(max_length=6000)] = ""

    @model_validator(mode="after")
    def unique_facts(self):
        if len({fact.id for fact in self.facts}) != len(self.facts):
            raise ValueError("Duplicate fact identifiers")
        if any(not fact.text.strip() for fact in self.facts):
            raise ValueError("Fact text cannot be blank")
        return self


class Activation(Input):
    expected_version: Text | None
    confirmed: Literal[True]


class Conversation(Input):
    template_id: Text | None = None
    job_id: Text | None = None


class Message(Input):
    text: Annotated[str, Field(min_length=1, max_length=6000)]
    request_id: Text


class Generation(Input):
    kind: Literal["cv", "cover_letter"]
    language: Literal["en", "auto"]
    request_id: Text


def public_template(template: dict | None) -> dict | None:
    if template is None:
        return None
    result = {
        key: value for key, value in template.items() if key not in {"path", "style", "reference"}
    }
    if reference := template.get("reference"):
        result["reference"] = {key: reference[key] for key in ("sha256", "page_count")}
    return result


def job_context(job: dict) -> dict:
    return {
        key: value
        for key, value in job.items()
        if key not in {"documents", "applied_at", "archived_at"}
    }


async def changes(request: Request, security: Security, token: str):
    previous = None
    heartbeat = 0.0
    while not await request.is_disconnected():
        if not security.session(token):
            return
        revision = security.store.get_setting("revision", 0)
        if revision != previous:
            yield f"event: change\ndata: {encode({'revision': revision})}\n\n"
            previous = revision
        elif time.monotonic() - heartbeat > 20:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic()
        await asyncio.sleep(1)


def create_app(settings: WebSettings | None = None, store: WebStore | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    store = store or WebStore(settings.data_dir / "jobs.sqlite3")
    store.initialize()
    security = Security(store, settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.security, app.state.settings = store, security, settings

    @app.exception_handler(StoreError)
    async def store_error(request: Request, error: StoreError):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.exception_handler(DocumentFileError)
    async def document_error(request: Request, error: DocumentFileError):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        try:
            host = request.headers.get("host", "")
            if host not in settings.hosts:
                raise StoreError("Недопустимый адрес приложения.", 400)
            mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
            if mutation:
                if request.headers.get("origin", "") not in settings.origins:
                    raise StoreError("Недопустимый источник запроса.", 403)
                length = request.headers.get("content-length", "0")
                if (
                    not length.isdecimal()
                    or len(length) > len(str(MAX_BODY))
                    or int(length) > MAX_BODY
                ):
                    raise StoreError("Слишком большой запрос.", 413)
                total, parts = 0, []
                async for part in request.stream():
                    total += len(part)
                    if total > MAX_BODY:
                        raise StoreError("Слишком большой запрос.", 413)
                    parts.append(part)
                request._body = b"".join(parts)
            if request.url.path.startswith("/api/") and request.url.path not in {
                "/api/session",
                "/api/login",
                "/api/setup",
            }:
                security.authorize(
                    request.cookies.get(COOKIE, ""),
                    request.headers.get("x-csrf-token", "") if mutation else None,
                )
            response = await call_next(request)
        except StoreError as error:
            response = JSONResponse({"detail": str(error)}, status_code=error.status)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'"
                ),
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    def session_response(request: Request):
        security.logout(request.cookies.get(COOKIE, ""))
        token, csrf = security.new_session()
        response = JSONResponse(
            {"authenticated": True, "setup_required": False, "csrf_token": csrf}
        )
        secure = f"https://{request.headers['host']}" in settings.origins
        response.set_cookie(
            COOKIE, token, max_age=86400, httponly=True, secure=secure, samesite="strict"
        )
        return response

    @app.get("/api/session")
    def session(request: Request):
        current = security.session(request.cookies.get(COOKIE, ""))
        return {
            "authenticated": bool(current),
            "setup_required": not bool(store.get_setting("password")),
            "csrf_token": current["csrf"] if current else None,
        }

    @app.post("/api/setup")
    def setup(data: Setup, request: Request):
        security.setup(data.token, data.password)
        return session_response(request)

    @app.post("/api/login")
    def login(data: Login, request: Request):
        security.login(data.password, request.client.host if request.client else "unknown")
        return session_response(request)

    @app.post("/api/logout")
    def logout(request: Request):
        security.logout(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/jobs")
    def jobs(
        tab: str = "active",
        page: int = 1,
        snapshot_id: str | None = None,
        page_size: Annotated[int, Query(ge=10, le=10)] = 10,
    ):
        return store.list_jobs(tab, page, snapshot_id)

    @app.patch("/api/jobs/{job_id}/state")
    def state(job_id: str, data: State):
        return store.change_state(job_id, data.model_dump(exclude_none=True))

    @app.post("/api/search-runs", status_code=202)
    def search():
        return store.enqueue("search", "search", {}, str(uuid4()))

    @app.get("/api/tasks")
    def tasks():
        return {"items": store.tasks()}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return store.task(task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return store.cancel_task(task_id)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str):
        store.document_task_for_deletion(task_id)
        staged = stage_task_directory(settings, task_id)
        if staged:
            try:
                shutil.rmtree(staged[1])
            except OSError as error:
                if staged[1].exists():
                    staged[1].rename(staged[0])
                raise StoreError("Не удалось удалить файлы документа.", 500) from error
        store.delete_document_task(task_id)
        return Response(status_code=204)

    @app.get("/api/tasks/{task_id}/review")
    def review(task_id: str):
        return store.task(task_id)["result"]

    @app.get("/api/tasks/{task_id}/document-preview")
    def document_preview(task_id: str):
        task = store.task(task_id, internal=True)
        result = task["result"]
        draft = result.get("approved_draft")
        if task["status"] != "ready" or not result.get("verified") or not isinstance(draft, dict):
            raise StoreError("Проверенный черновик недоступен.", 404)
        if content_hash(draft) != result.get("draft_hash"):
            raise StoreError("Проверка целостности черновика не пройдена.", 409)
        try:
            used_ids = {
                fact_id
                for section in draft["sections"]
                for paragraph in section["paragraphs"]
                for fact_id in paragraph["fact_ids"]
            }
            pinned = task["payload"]["context"]["template"]["facts"]
            facts = [
                {key: fact[key] for key in ("id", "text", "source") if key in fact}
                for fact in pinned
                if fact["id"] in used_ids
            ]
            if {fact["id"] for fact in facts} != used_ids:
                raise StoreError("Подтверждённые факты документа недоступны.", 409)
        except (KeyError, TypeError) as error:
            raise StoreError("Подтверждённые факты документа недоступны.", 409) from error
        return {"draft": draft, "facts": facts}

    @app.get("/api/profile")
    def profile():
        data = store.profile()
        return {
            "active": public_template(data["active"]),
            "drafts": [public_template(draft) for draft in data["drafts"]],
        }

    @app.post("/api/template-drafts", status_code=201)
    async def upload(file: UploadFile):
        content = await file.read(10 * 1024 * 1024 + 1)
        await file.close()
        if len(content) > 10 * 1024 * 1024:
            raise StoreError("Максимальный размер шаблона — 10 МБ.", 413)
        filename = Path(file.filename or "template").name
        parsed = await asyncio.to_thread(parse_template, content, filename)
        directory = settings.data_dir / "templates"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{uuid4()}{Path(filename).suffix.lower()}"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        return public_template(
            store.add_template(
                {
                    **parsed,
                    "filename": filename,
                    "path": str(path.resolve()),
                    "source_sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )

    def template_source(template: dict) -> Path:
        path = Path(template.get("path", ""))
        allowed = (settings.data_dir / "templates").resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(allowed) or not path.is_file():
            raise StoreError("Исходный файл шаблона недоступен.", 404)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise StoreError("Недопустимый размер исходного файла.", 422)
        expected = template.get("source_sha256") or (template.get("reference") or {}).get("sha256")
        if expected and not hmac.compare_digest(
            hashlib.sha256(path.read_bytes()).hexdigest(), expected
        ):
            raise StoreError("Исходный файл шаблона изменён. Загрузите его заново.", 409)
        return path

    @app.get("/api/templates/{template_id}/source")
    def source(template_id: str):
        template = store.template(template_id)
        return FileResponse(template_source(template), filename=template["filename"])

    @app.get("/api/templates/{template_id}/pages/{page}")
    def template_page(template_id: str, page: int):
        template = store.template(template_id)
        pages = (template.get("reference") or {}).get("pages", [])
        if page < 1 or page > len(pages):
            raise StoreError("Страница эталона не найдена.", 404)
        return Response(
            base64.b64decode(pages[page - 1]["image_base64"], validate=True),
            media_type="image/png",
        )

    @app.post("/api/templates/{template_id}/reparse", status_code=201)
    async def reparse(template_id: str):
        template = store.template(template_id)
        path = template_source(template)
        if path.suffix.lower() != ".pdf":
            raise StoreError("Повторное чтение доступно для PDF.", 422)
        content = path.read_bytes()
        parsed = await asyncio.to_thread(parse_template, content, template["filename"])
        return public_template(
            store.add_template(
                {
                    **parsed,
                    "filename": template["filename"],
                    "path": str(path.resolve()),
                    "source_sha256": hashlib.sha256(content).hexdigest(),
                    "instructions": template.get("instructions", ""),
                }
            )
        )

    @app.patch("/api/template-drafts/{template_id}")
    def update(template_id: str, data: TemplateChange):
        return public_template(
            store.update_template(template_id, data.model_dump(exclude_unset=True))
        )

    @app.post("/api/template-drafts/{template_id}/activate")
    def activate(template_id: str, data: Activation):
        return public_template(store.activate_template(template_id, data.expected_version))

    @app.post("/api/conversations")
    def conversation(data: Conversation):
        return store.conversation(data.template_id, data.job_id)

    @app.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str):
        return {"items": store.messages(conversation_id)}

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    def message(conversation_id: str, data: Message):
        if not data.text.strip():
            raise StoreError("Введите сообщение.", 422)
        request = {
            "operation": "chat",
            "conversation_id": conversation_id,
            "text": data.text.strip(),
        }
        existing = store.request_task(data.request_id, request)
        if existing:
            return existing
        conversation = store.get_conversation(conversation_id)
        template = (
            store.template(conversation["template_id"])
            if conversation["template_id"]
            else store.profile()["active"]
        )
        if template is None:
            raise StoreError("Сначала задайте эталонный шаблон.", 409)
        job = job_context(store.get_job(conversation["job_id"])) if conversation["job_id"] else {}
        return store.enqueue_message(
            conversation_id, {"job": job, "template": template}, data.text.strip(), data.request_id
        )

    @app.post("/api/jobs/{job_id}/documents", status_code=202)
    def generate(job_id: str, data: Generation):
        if (data.kind == "cv" and data.language != "en") or (
            data.kind == "cover_letter" and data.language != "auto"
        ):
            raise StoreError("CV создаётся на английском, cover letter — на языке вакансии.", 422)
        request = {
            "operation": "document",
            "job_id": job_id,
            "kind": data.kind,
            "language": data.language,
        }
        existing = store.request_task(data.request_id, request)
        if existing:
            return existing
        job = job_context(store.get_job(job_id))
        template = store.profile()["active"]
        if template is None:
            raise StoreError("Сначала задайте и подтвердите эталонный шаблон.", 409)
        context = {
            "job": job,
            "template": template,
            "owner_messages": store.owner_messages(job_id),
            "conversation_messages": store.document_conversation(job_id),
        }
        return store.enqueue(
            data.kind,
            f"{data.kind}:{job_id}:{digest(context)}",
            {"job_id": job_id, "context": context},
            data.request_id,
            request=request,
        )

    def verified_artifact(artifact_id: str) -> tuple[dict, Path]:
        artifact = store.artifact(artifact_id)
        path = Path(artifact["path"]).resolve()
        task = store.task(artifact["task_id"])
        allowed = (settings.data_dir / "artifacts" / task["id"]).resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise StoreError("Файл недоступен.", 404)
        if (
            artifact_id not in task["result"].get("artifact_ids", [])
            or not hmac.compare_digest(artifact["draft_hash"], task["result"].get("draft_hash", ""))
            or not hmac.compare_digest(
                hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"]
            )
        ):
            raise StoreError("Проверка целостности документа не пройдена.", 409)
        return artifact, path

    @app.get("/api/artifacts/{artifact_id}/download")
    def download(artifact_id: str):
        artifact, path = verified_artifact(artifact_id)
        return FileResponse(
            path,
            filename=artifact["filename"],
            media_type="application/pdf"
            if artifact["format"] == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    @app.get("/api/artifacts/{artifact_id}/preview")
    def preview_artifact(artifact_id: str):
        artifact, path = verified_artifact(artifact_id)
        if artifact["format"] != "pdf":
            raise StoreError("Просмотр доступен только для PDF.", 422)
        return FileResponse(
            path,
            filename=artifact["filename"],
            media_type="application/pdf",
            content_disposition_type="inline",
        )

    @app.get("/api/events")
    async def events(request: Request):
        return StreamingResponse(
            changes(request, security, request.cookies[COOKIE]),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/{path:path}")
    def frontend(path: str):
        target = (settings.frontend_dir / (path or "index.html")).resolve()
        if target.is_relative_to(settings.frontend_dir.resolve()) and target.is_file():
            return FileResponse(target)
        return JSONResponse({"detail": "Страница не найдена."}, status_code=404)

    return app


def main() -> None:
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=int(os.getenv("WEB_PORT", "8000")),
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
