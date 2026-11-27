from typing import Annotated, Any, Literal

from fastapi import Depends, File, Query, Request, UploadFile
from pydantic import Field

from qa_job_bot.geography import find_places
from qa_job_bot.models import utcnow
from qa_job_bot.search_models import (
    AGGREGATORS,
    SearchSpec,
    SourceSpec,
    StrictInput,
    clean_text,
    search_examples,
    source_identity,
)
from qa_job_bot.source_catalog import catalog, probe_source
from qa_job_bot.source_http import SourceClient
from qa_job_bot.source_import import MAX_CSV_BYTES, csv_rows, link_spec, sheets_csv
from qa_job_bot.web_store import StoreError


def personal_store(request: Request):
    return request.state.store


PersonalStore = Annotated[Any, Depends(personal_store)]


class ProfileChange(StrictInput):
    expected_version: Annotated[int, Field(ge=0)]
    profile: SearchSpec
    expected_editor_version: Annotated[int, Field(ge=0)] | None = None
    variant_id: Annotated[str, Field(max_length=40)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    create: bool = False


class VariantChange(StrictInput):
    expected_editor_version: Annotated[int, Field(ge=0)]
    action: Literal["activate", "rename", "delete"]
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None


class SourceChange(StrictInput):
    expected_version: Annotated[int, Field(ge=1)]
    enabled: bool | None = None
    config: dict | None = None


class ExpectedVersion(StrictInput):
    expected_version: Annotated[int, Field(ge=1)]


class SourceLink(StrictInput):
    url: Annotated[str, Field(min_length=1, max_length=2048)]


class SourceBatch(StrictInput):
    items: Annotated[list[SourceSpec], Field(min_length=1, max_length=100)]


def profile_name(value):
    if value is None:
        return None
    try:
        return clean_text(value)
    except ValueError as error:
        raise StoreError(str(error), 422) from None


def register_preferences_routes(app):
    @app.get("/api/search-settings")
    def preferences(store: PersonalStore):
        return store.preferences()

    @app.get("/api/search-profile")
    def profile(store: PersonalStore):
        return store.preferences()["search_profile"]

    @app.put("/api/search-profile")
    def save_profile(data: ProfileChange, store: PersonalStore):
        return store.save_search_profile(
            data.profile,
            data.expected_version,
            expected_editor_version=data.expected_editor_version,
            variant_id=data.variant_id,
            name=profile_name(data.name),
            create=data.create,
        )

    @app.post("/api/search-profile/history/{version}")
    def update_profile_variant(version: int, data: VariantChange, store: PersonalStore):
        name = profile_name(data.name)
        if data.action == "rename" and not name:
            raise StoreError("Укажите имя варианта.", 422)
        return store.change_profile_variant(
            version, data.action, data.expected_editor_version, name
        )

    @app.get("/api/places")
    def places(q: Annotated[str, Query(max_length=120)] = "", kind: str = ""):
        return find_places(q, tuple(kind.split(",")) if kind else ())

    @app.get("/api/places/legacy")
    def legacy_places(value: Annotated[list[str], Query(max_length=200)]):
        from qa_job_bot.geography import exact_places, place_label

        return {
            text: [{"id": key, "label": place_label(key)} for key in sorted(exact_places(text))]
            for text in value
        }

    @app.get("/api/search-profile/history")
    def profile_history(store: PersonalStore):
        return {"items": store.search_profile_history()}

    @app.get("/api/source-types")
    def source_types():
        return {"items": catalog()}

    @app.get("/api/search-presets")
    def presets():
        return {"items": search_examples()}

    @app.get("/api/sources")
    def sources(store: PersonalStore):
        return store.preferences()["sources"]

    @app.post("/api/sources", status_code=201)
    def add_source(data: SourceSpec, store: PersonalStore):
        return store.add_source(data)

    @app.post("/api/sources/recommended")
    def add_recommended_sources(store: PersonalStore):
        return store.add_source_set([SourceSpec(kind=kind) for kind in AGGREGATORS])

    @app.post("/api/sources/resolve")
    def resolve_source(data: SourceLink, store: PersonalStore):
        try:
            spec = link_spec(data.url.strip())
        except ValueError as error:
            raise StoreError(str(error), 422) from None
        key = source_identity(spec.kind, spec.normalized())
        existing = next(
            (
                row
                for row in store.preferences()["sources"]["items"]
                if row["valid"] and source_identity(row["kind"], row["config"]) == key
            ),
            None,
        )
        return {"spec": spec.model_dump(), "existing": existing}

    @app.post("/api/sources/import/file")
    async def preview_source_file(
        store: PersonalStore,
        file: Annotated[UploadFile, File()],
    ):
        try:
            raw = await file.read(MAX_CSV_BYTES + 1)
        finally:
            await file.close()
        return {"items": csv_rows(raw, store.source_import_identities())}

    @app.post("/api/sources/import/link")
    async def preview_source_link(data: SourceLink, request: Request, store: PersonalStore):
        factory = getattr(
            request.app.state,
            "source_client_factory",
            lambda: SourceClient(max_requests=3, max_response=MAX_CSV_BYTES),
        )
        raw = await sheets_csv(data.url.strip(), factory)
        return {"items": csv_rows(raw, store.source_import_identities())}

    @app.post("/api/sources/import")
    def confirm_source_import(data: SourceBatch, store: PersonalStore):
        return store.add_source_set(data.items)

    @app.patch("/api/sources/{source_id}")
    def update_source(source_id: str, data: SourceChange, store: PersonalStore):
        return store.update_source(source_id, data.expected_version, data.enabled, data.config)

    @app.delete("/api/sources/{source_id}", status_code=204)
    def delete_source(source_id: str, data: ExpectedVersion, store: PersonalStore):
        store.delete_source(source_id, data.expected_version)

    @app.get("/api/sources/{source_id}/history")
    def source_history(source_id: str, store: PersonalStore):
        return {"items": store.source_history(source_id)}

    @app.post("/api/sources/{source_id}/check")
    async def check_source(
        source_id: str, data: ExpectedVersion, request: Request, store: PersonalStore
    ):
        source, profile = store.source_for_check(source_id, data.expected_version)
        queries = (profile["profile"] or {}).get("source_queries") or (
            profile["profile"] or {}
        ).get("role_keywords", [])
        result = await probe_source(
            SourceSpec(kind=source["kind"], config=source["config"]),
            queries,
            getattr(request.app.state, "source_client_factory", SourceClient),
        )
        current = store.preferences()
        version = next(
            (row["version"] for row in current["sources"]["items"] if row["id"] == source_id), None
        )
        return {
            **result,
            "source_version": source["version"],
            "profile_version": profile["version"],
            "checked_at": utcnow().isoformat(),
            "limitations": source["limitations"],
            "stale": version != source["version"]
            or current["search_profile"]["generation"] != profile["generation"],
        }
