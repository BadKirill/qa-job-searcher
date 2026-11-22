from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import Field

from qa_job_bot.search_models import SearchSpec, SourceSpec, StrictInput, search_presets
from qa_job_bot.source_catalog import catalog, probe_source
from qa_job_bot.source_http import SourceClient


def personal_store(request: Request):
    return request.state.store


PersonalStore = Annotated[Any, Depends(personal_store)]


class ProfileChange(StrictInput):
    expected_version: Annotated[int, Field(ge=0)]
    profile: SearchSpec


class SourceChange(StrictInput):
    expected_version: Annotated[int, Field(ge=1)]
    enabled: bool | None = None
    config: dict | None = None


class ExpectedVersion(StrictInput):
    expected_version: Annotated[int, Field(ge=1)]


class PresetChange(StrictInput):
    expected_version: Annotated[int, Field(ge=0)]
    expected_sources_version: Annotated[int, Field(ge=0)]


def register_preferences_routes(app):
    @app.get("/api/search-settings")
    def preferences(store: PersonalStore):
        return store.preferences()

    @app.get("/api/search-profile")
    def profile(store: PersonalStore):
        return store.preferences()["search_profile"]

    @app.put("/api/search-profile")
    def save_profile(data: ProfileChange, store: PersonalStore):
        return store.save_search_profile(data.profile, data.expected_version)

    @app.get("/api/search-profile/history")
    def profile_history(store: PersonalStore):
        return {"items": store.search_profile_history()}

    @app.get("/api/source-types")
    def source_types():
        return {"items": catalog()}

    @app.get("/api/search-presets")
    def presets():
        return {"items": search_presets()}

    @app.post("/api/search-presets/{name}/apply")
    def apply_preset(name: str, data: PresetChange, store: PersonalStore):
        return store.apply_search_preset(name, data.expected_version, data.expected_sources_version)

    @app.get("/api/sources")
    def sources(store: PersonalStore):
        return store.preferences()["sources"]

    @app.post("/api/sources", status_code=201)
    def add_source(data: SourceSpec, store: PersonalStore):
        return store.add_source(data)

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
            "limitations": source["limitations"],
            "stale": version != source["version"]
            or current["search_profile"]["version"] != profile["version"],
        }
