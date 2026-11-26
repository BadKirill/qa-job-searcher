from typing import Annotated
from urllib.parse import urlunsplit

from fastapi import Request
from pydantic import Field

from qa_job_bot.posting_details import (
    MAX_DESCRIPTION,
    MIN_DESCRIPTION,
    description_text,
    fetch_posting,
)
from qa_job_bot.preferences_routes import PersonalStore
from qa_job_bot.search_models import StrictInput
from qa_job_bot.source_http import SourceClient
from qa_job_bot.source_links import https_parts, posting_link
from qa_job_bot.web_store import StoreError


class PostingPreview(StrictInput):
    url: Annotated[str, Field(min_length=1, max_length=2048)]


class ManualContent(StrictInput):
    title: Annotated[str, Field(min_length=1, max_length=240)]
    company: Annotated[str, Field(min_length=1, max_length=240)]
    location: Annotated[str, Field(max_length=240)] = ""
    description: Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION)]
    url: Annotated[str, Field(max_length=2048)] = ""

    def normalized(self):
        data = {
            key: " ".join(value.split())
            for key, value in self.model_dump().items()
            if key in {"title", "company", "location"}
        }
        data["description"] = description_text(self.description)
        if not all(data[key] for key in ("title", "company")):
            raise StoreError("Укажите название вакансии и компанию.", 422)
        if len(data["description"]) < MIN_DESCRIPTION:
            raise StoreError("Добавьте описание задач и требований вакансии.", 422)
        data["url"] = ""
        if self.url.strip():
            try:
                url, _ = https_parts(self.url.strip())
                link = posting_link(self.url.strip())
                data["url"] = (
                    link.url
                    if link
                    else urlunsplit(("https", url.hostname, url.path or "/", url.query, ""))
                )
            except ValueError as error:
                raise StoreError(str(error), 422) from None
        data["description_quality"] = "provided"
        return data


class ManualSave(ManualContent):
    request_id: Annotated[str, Field(min_length=1, max_length=120)]


class PostingCheck(StrictInput):
    request_id: Annotated[str, Field(min_length=1, max_length=120)]


class ManualEdit(ManualContent):
    expected_version: Annotated[int, Field(ge=1)]


def source_factory(request):
    return getattr(
        request.app.state,
        "source_client_factory",
        lambda: SourceClient(max_requests=3, max_response=4 * 1024 * 1024),
    )


def register_posting_routes(app):
    @app.post("/api/manual-jobs/preview")
    async def preview_job(data: PostingPreview, request: Request, store: PersonalStore):
        store.reserve_posting_preview()
        result = await fetch_posting(data.url.strip(), source_factory(request))
        return {"posting": result.public_status(), "job": result.job}

    @app.post("/api/manual-jobs", status_code=201)
    def save_job(data: ManualSave, store: PersonalStore):
        return store.save_manual_job(data.normalized(), data.request_id)

    @app.patch("/api/manual-jobs/{job_id}")
    def edit_job(job_id: str, data: ManualEdit, store: PersonalStore):
        return store.revise_manual_job(job_id, data.normalized(), data.expected_version)

    @app.post("/api/jobs/{job_id}/check-posting", status_code=202)
    def check_posting(job_id: str, data: PostingCheck, store: PersonalStore):
        return store.enqueue_posting_check(job_id, data.request_id)
