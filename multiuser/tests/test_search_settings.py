import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import httpx
import pytest
from baseline_fixtures import BASELINE, baseline_job, baseline_profile
from pg_fixtures import personal_store
from pydantic import ValidationError
from test_accounts import api_world as api_world

from qa_job_bot import pg_schema as s
from qa_job_bot.config import SearchProfile
from qa_job_bot.matching import match_job
from qa_job_bot.models import Job
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.search_models import (
    AGGREGATORS,
    KINDS,
    SearchSpec,
    SourceSpec,
    clean_text,
    search_examples,
    universal_profile,
)
from qa_job_bot.source_catalog import build_personal_sources
from qa_job_bot.universal_matching import workplace
from qa_job_bot.web_store import StoreError
from qa_job_bot.worker import Worker, build_search


def spec(**changes):
    return SearchSpec(role_keywords=["backend engineer"], **changes)


def vacancy(**changes):
    values = dict(
        source="Synthetic",
        source_id="backend",
        title="Junior Backend Engineer",
        company="Acme",
        location="Worldwide",
        url="https://example.test/backend",
        description="Python software development.",
        remote=True,
        workplace_type="remote",
    )
    return Job(**{**values, **changes})


@pytest.mark.parametrize("value", [None, 12, "", "  ", "a" * 121, "bad\nvalue", "\ud800"])
def test_terms_reject_malformed_input(value):
    with pytest.raises(ValueError):
        clean_text(value)


def test_search_spec_is_explicit_and_normalized():
    profile = SearchSpec(
        role_keywords=["  Ｂackend Engineer ", "backend engineer"],
        workplace_types=["hybrid", "hybrid"],
        working_languages=["ru", "ru"],
    )
    assert profile.role_keywords == ["backend engineer"]
    assert profile.workplace_types == ["hybrid"]
    runtime = profile.runtime()
    assert not runtime.exclude_entry_level and not runtime.qa_title_fallback
    assert runtime.excluded_title_keywords == () and runtime.allowed_locations == ()
    assert runtime.working_languages == ("ru",)
    assert spec(working_languages=["ru", "en"]).working_languages == ["en", "ru"]
    for changes in (
        {"role_keywords": []},
        {"role_keywords": "backend"},
        {"source_queries": ["a"] * 9},
        {"min_score": True},
        {"min_score": 101},
        {"workplace_types": []},
        {"workplace_types": ["office"]},
        {"working_languages": ["de"]},
        {"working_languages": "ru"},
        {"working_languages": ["ru", "en", "en"]},
        {"extra": "x"},
    ):
        with pytest.raises(ValidationError):
            SearchSpec.model_validate({"role_keywords": ["backend"], **changes})


@pytest.mark.parametrize(
    "kind,config",
    [
        ("remotive", {"url": "http://127.0.0.1"}),
        ("telegram", {}),
        ("telegram", {"handle": "https://t.me/test"}),
        ("telegram", {"handle": "bad"}),
        ("telegram", {"handle": "test_channel", "url": "x"}),
        ("lever", {}),
        ("ashby", {"token": "../private"}),
        ("greenhouse", {"token": "company?x=1"}),
        ("lever", {"token": "company", "region": "us"}),
        ("ashby", {"token": "company", "region": "eu"}),
        ("lever", {"token": "company", "tags": "text"}),
        ("lever", {"token": "company", "tags": ["tag"] * 13}),
        ("lever", {"token": "company", "name": "\ud800"}),
        ("lever", {"token": "company", "url": "https://example.test"}),
    ],
)
def test_sources_accept_identifiers_instead_of_arbitrary_urls(kind, config):
    with pytest.raises(ValueError):
        SourceSpec(kind=kind, config=config).normalized()


def test_personal_factory_uses_only_enabled_sources_and_personal_queries():
    rows = [
        {
            "kind": kind,
            "enabled": True,
            "config": {}
            if index < 5
            else {"handle": "@test_channel"}
            if kind == "telegram"
            else {"token": "company"},
        }
        for index, kind in enumerate(KINDS)
    ]
    rows.append({"kind": "lever", "enabled": False, "config": {}})
    sources = build_personal_sources(rows, spec(source_queries=["engineer"]).runtime())
    assert len(sources) == 9
    assert sources[1].queries == ("engineer",) and sources[3].queries == ("engineer",)
    assert sources[-1].role_keywords == ("backend engineer",)
    templates = search_examples()["items"]
    qa = build_personal_sources(rows, SearchSpec.model_validate(templates["qa"]).runtime())
    assert "sdet" in qa[-1].role_keywords
    assert build_personal_sources([], spec().runtime()) == []
    assert build_personal_sources(rows, spec().runtime())[1].queries == ("backend engineer",)
    assert universal_profile(templates["developer"]) == templates["developer"]


@pytest.mark.parametrize(
    "case", [c for c in BASELINE["cases"] if c["profile"] == "qa"], ids=lambda c: c["id"]
)
def test_explicit_qa_preset_preserves_owner_baseline(case):
    profile = SearchSpec.model_validate(universal_profile(asdict(baseline_profile("qa")))).runtime()
    result = match_job(baseline_job(case), profile)
    assert result.accepted == case["baseline"]["accepted"]


def test_profession_junior_and_workplace_are_personal_choices():
    job = vacancy()
    assert match_job(job, spec().runtime()).accepted
    assert not match_job(job, spec(exclude_entry_level=True).runtime()).accepted
    assert not match_job(vacancy(title="Senior QA Engineer"), spec().runtime()).accepted
    assert match_job(
        vacancy(title="Senior QA Engineer"), spec(qa_title_fallback=True).runtime()
    ).accepted
    hybrid = vacancy(location="Berlin, Germany", remote=False, workplace_type="hybrid")
    assert not match_job(hybrid, spec().runtime()).accepted
    profile = spec(workplace_types=["hybrid"], hybrid_locations=["berlin"]).runtime()
    assert match_job(hybrid, profile).accepted
    assert not match_job(replace(hybrid, location="Belgrade"), profile).accepted
    assert match_job(
        replace(hybrid, workplace_type="on-site"), spec(workplace_types=["on-site"]).runtime()
    ).accepted


@pytest.mark.parametrize(
    "job,settings,expected",
    [
        (
            {"description": "Remote worldwide except Germany."},
            {"candidate_locations": ["germany"]},
            False,
        ),
        ({"description": "Germany is not supported."}, {"candidate_locations": ["germany"]}, False),
        ({"excluded_locations": ("Germany",)}, {"candidate_locations": ["germany"]}, False),
        (
            {"location_restrictions": ("United States",)},
            {"candidate_locations": ["germany"]},
            False,
        ),
        (
            {"location_restrictions": ("Europe",)},
            {"candidate_locations": ["germany"], "allowed_locations": ["europe"]},
            False,
        ),
        (
            {"description": "Candidates must reside in Germany."},
            {"candidate_locations": ["serbia"], "allowed_locations": ["europe"]},
            False,
        ),
        ({"location": "India"}, {"allowed_locations": ["europe"]}, False),
        ({"location": "India"}, {"allowed_locations": ["india"]}, True),
        ({"location": "United States"}, {"blocked_locations": ["united states"]}, False),
        (
            {"location": "Worldwide", "description": "US-based team."},
            {"allowed_locations": ["worldwide"], "blocked_locations": ["us"]},
            True,
        ),
        (
            {"location": "Remote", "description": "No details."},
            {"allowed_locations": ["europe"]},
            False,
        ),
        (
            {"location": "Remote", "description": "No details."},
            {"allowed_locations": ["europe"], "allow_unknown_remote_location": True},
            True,
        ),
    ],
)
def test_universal_geography_uses_full_restrictions(job, settings, expected):
    assert match_job(vacancy(**job), spec(**settings).runtime()).accepted is expected


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"workplace_type": "office"}, "on-site"),
        ({"workplace_type": "onsite"}, "on-site"),
        ({"workplace_type": "", "description": "Hybrid role"}, "hybrid"),
        ({"workplace_type": "", "remote": None, "description": "Fully remote role"}, "remote"),
        ({"workplace_type": "", "remote": None, "location": "Remote"}, "remote"),
        ({"workplace_type": "", "remote": False, "location": "Berlin"}, "on-site"),
        (
            {
                "workplace_type": "",
                "remote": None,
                "location": "Berlin",
                "description": "Office work",
            },
            "on-site",
        ),
        ({"workplace_type": "", "remote": None, "location": "Not specified"}, "unknown"),
    ],
)
def test_workplace_inference(data, expected):
    assert workplace(vacancy(**data)) == expected


def test_pg_preferences_history_ownership_and_compare_and_swap(pg_store):
    other = personal_store(pg_store.database, "another")
    assert (
        pg_store.preferences()
        == other.preferences()
        == {
            "search_profile": {
                "version": 0,
                "generation": 0,
                "editor_version": 0,
                "configured": False,
                "profile": None,
            },
            "sources": {"version": 0, "items": []},
        }
    )
    for action in [
        lambda: pg_store.search_configuration(),
        lambda: pg_store.save_search_profile(spec(), 1),
    ]:
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.status == 409
    saved = pg_store.save_search_profile(spec(), 0)
    assert saved["version"] == 1
    with pytest.raises(StoreError):
        pg_store.search_configuration()
    source = pg_store.add_source(SourceSpec(kind="lever", config={"token": "company"}))
    assert pg_store.search_configuration()[0].role_keywords == ("backend engineer",)
    assert source["version"] == 1
    assert pg_store.update_source(source["id"], 1, enabled=False)["version"] == 2
    with pytest.raises(StoreError):
        pg_store.update_source(source["id"], 1, enabled=True)
    pg_store.update_source(source["id"], 2, config={"token": "renamed", "region": "eu"})
    for action in [
        lambda: other.update_source(source["id"], 3),
        lambda: other.delete_source(source["id"], 3),
        lambda: other.source_history(source["id"]),
    ]:
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.status == 404
    pg_store.save_search_profile(spec(workplace_types=["hybrid"]), 1)
    history = pg_store.search_profile_history()
    assert [r["version"] for r in history] == [1, 2]
    assert [r["active"] for r in history] == [False, True]
    pg_store.delete_source(source["id"], 3)
    history = pg_store.source_history(source["id"])
    assert [r["version"] for r in history] == [1, 2, 3, 4]
    assert history[-1]["deleted"] and history[0]["payload"]["config"]["token"] == "company"
    assert other.search_profile_history() == [] and not other.preferences()["sources"]["items"]


def test_pg_recommended_sources_are_idempotent_and_do_not_enable_existing_sources(pg_store):
    board = pg_store.add_source(SourceSpec(kind="ashby", config={"token": "mine"}))
    disabled = pg_store.add_source(SourceSpec(kind="remotive", enabled=False))
    recommended = [SourceSpec(kind=kind) for kind in AGGREGATORS]
    assert len(pg_store.add_source_set(recommended)["items"]) == 4
    state = pg_store.preferences()
    assert len(state["sources"]["items"]) == 6
    assert (
        next(r for r in state["sources"]["items"] if r["id"] == disabled["id"])["enabled"] is False
    )
    assert (
        next(r for r in state["sources"]["items"] if r["id"] == board["id"])["config"]["token"]
        == "mine"
    )
    assert pg_store.add_source_set(recommended)["items"] == []
    assert pg_store.preferences()["sources"]["version"] == 3
    assert not pg_store.preferences()["search_profile"]["configured"]
    with pytest.raises(StoreError):
        pg_store.add_source(SourceSpec(kind="ashby", config={"token": "MINE"}))
    assert (
        PostgresStore(pg_store.database, pg_store.user_id).preferences()["sources"]["items"][0][
            "enabled"
        ]
        is not None
    )


def test_parallel_profile_and_source_edits_have_one_winner(pg_store):
    source = pg_store.add_source(SourceSpec(kind="remotive"))

    def save(index):
        try:
            pg_store.save_search_profile(spec(min_score=index), 0)
            return 200
        except StoreError as error:
            return error.status

    def toggle(index):
        try:
            pg_store.update_source(source["id"], 1, enabled=bool(index))
            return 200
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(save, range(4))) == [200, 409, 409, 409]
        assert sorted(pool.map(toggle, range(4))) == [200, 409, 409, 409]


def test_imported_settings_remain_editable_with_original_history(pg_store):
    original_profile = asdict(SearchProfile())
    del original_profile["working_languages"]
    with pg_store.write() as connection:
        connection.execute(
            s.search_profiles.insert().values(
                id="old-profile",
                user_id=pg_store.user_id,
                version=1,
                active=True,
                payload=original_profile,
            )
        )
        connection.execute(s.source_definitions.insert().values(kind="lever", payload={}))
        connection.execute(
            s.user_sources.insert().values(
                id="old-source",
                user_id=pg_store.user_id,
                kind="lever",
                enabled=True,
                version=1,
                payload={"kind": "lever", "name": "Old", "token": "old", "tags": []},
            )
        )
    active_profile = pg_store.preferences()["search_profile"]["profile"]
    assert active_profile["qa_title_fallback"] and active_profile["working_languages"] == []
    assert len(pg_store.source_history("old-source")) == 1
    pg_store.update_source("old-source", 1, enabled=False)
    assert len(pg_store.source_history("old-source")) == 2
    pg_store.save_search_profile(spec(), 1)
    history = pg_store.search_profile_history()
    assert history[0]["profile"]["matching_mode"] == "legacy"
    assert "working_languages" not in history[0]["profile"]


def test_preferences_api_isolated_and_validated(api_world):
    admin, other, app, admin_id, _ = api_world
    assert other.get("/api/search-settings").json()["sources"]["items"] == []
    assert other.post("/api/search-runs").status_code == 409
    assert len(other.get("/api/source-types").json()["items"]) == 9
    catalog = other.get("/api/search-presets").json()
    assert len(catalog["items"]) == 14
    assert {"qa", "developer", "designer", "manual_qa", "qa_automation", "frontend"} <= set(
        catalog["items"]
    )
    assert catalog["items"].keys() == catalog["labels"].keys()
    assert len(set(catalog["labels"].values())) == len(catalog["items"])
    for profile in catalog["items"].values():
        template = SearchSpec.model_validate(profile)
        assert len(template.role_keywords) >= 4 and template.source_queries
        assert template.focus_qa_keywords and template.skill_keywords
        assert not template.candidate_locations and not template.excluded_title_keywords
        assert not template.working_languages and not template.exclude_entry_level
    assert "playwright" in catalog["items"]["qa_automation"]["skill_keywords"]
    assert "figma" in catalog["items"]["designer"]["skill_keywords"]
    assert not other.get("/api/search-profile/history").json()["items"]
    assert (
        other.put(
            "/api/search-profile", json={"expected_version": 0, "profile": spec().model_dump()}
        ).status_code
        == 200
    )
    source = other.post("/api/sources", json={"kind": "lever", "config": {"token": "mine"}}).json()
    path = "/api/sources/" + source["id"]
    assert other.post("/api/search-runs").status_code == 202
    assert admin.get(path + "/history").status_code == 404
    assert admin.patch(path, json={"expected_version": 1, "enabled": False}).status_code == 404
    assert admin.request("DELETE", path, json={"expected_version": 1}).status_code in {404, 422}
    assert other.patch(path, json={"expected_version": 1, "enabled": False}).status_code == 200
    assert other.patch(path, json={"expected_version": 1, "enabled": True}).status_code == 409
    assert (
        other.put(
            "/api/search-profile", json={"expected_version": 0, "profile": spec().model_dump()}
        ).status_code
        == 409
    )
    assert len(other.get("/api/search-profile/history").json()["items"]) == 1
    assert admin.get("/api/search-profile").json()["configured"] is False
    for body in (
        {"kind": "lever", "config": {"token": "https://127.0.0.1"}},
        {"kind": "telegram", "config": {"handle": "\ud800"}},
        {"kind": "remotive", "enabled": "true"},
        {"kind": "remotive", "user_id": admin_id},
        {"kind": "custom"},
    ):
        assert (
            other.post(
                "/api/sources",
                content=json.dumps(body),
                headers={"content-type": "application/json"},
            ).status_code
            == 422
        )
    assert (
        other.put(
            "/api/search-profile",
            content=json.dumps({"expected_version": 1, "profile": {"role_keywords": ["\ud800"]}}),
            headers={"content-type": "application/json"},
        ).status_code
        == 422
    )
    preset = other.get("/api/search-presets").json()["items"]["qa"]
    assert not preset["candidate_locations"]
    assert other.get("/api/search-settings").json()["search_profile"]["version"] == 1
    assert len(other.get("/api/sources").json()["items"]) == 1
    assert other.request("DELETE", path, json={"expected_version": 2}).status_code == 204
    assert other.get(path + "/history").json()["items"][-1]["deleted"]


def test_working_language_profile_api_defaults_validates_and_versions(api_world):
    admin, member, _, _, _ = api_world
    path = "/api/search-profile"
    previous_profile = {"role_keywords": ["backend engineer"]}
    first = member.put(path, json={"expected_version": 0, "profile": previous_profile})
    assert first.status_code == 200 and first.json()["profile"]["working_languages"] == []

    def revise(languages):
        return member.put(
            path,
            json={
                "expected_version": 1,
                "profile": {**previous_profile, "working_languages": languages},
            },
        )

    assert revise(["fr"]).status_code == 422
    saved = revise(["ru", "en"])
    assert saved.status_code == 200
    assert member.get(path).json()["profile"]["working_languages"] == ["en", "ru"]
    assert admin.get(path).json()["profile"] is None
    history = member.get(path + "/history").json()["items"]
    assert [item["profile"]["working_languages"] for item in history] == [[], ["en", "ru"]]


def test_source_check_api_never_publishes_and_marks_concurrent_edits_stale(api_world):
    admin, other, app, _, member_id = api_world
    source = other.post("/api/sources", json={"kind": "remotive"}).json()
    path = "/api/sources/" + source["id"]
    calls = []

    def response(request):
        calls.append(request)
        if len(calls) == 1:
            PostgresStore(app.state.database, member_id).update_source(
                source["id"], 1, enabled=False
            )
        return httpx.Response(200, json={"jobs": [{"title": "Backend Engineer"}]})

    from qa_job_bot.source_http import SourceClient

    app.state.source_client_factory = lambda **kwargs: SourceClient(
        transport=httpx.MockTransport(response), **kwargs
    )
    assert admin.post(path + "/check", json={"expected_version": 1}).status_code == 404
    result = other.post(path + "/check", json={"expected_version": 1})
    assert result.status_code == 200 and result.json()["stale"] and result.json()["items"] == 1
    assert len(calls) == 1 and other.get("/api/jobs").json()["total"] == 0
    assert other.post(path + "/check", json={"expected_version": 1}).status_code == 409
    for _ in range(4):
        assert other.post(path + "/check", json={"expected_version": 2}).status_code == 200
    assert other.post(path + "/check", json={"expected_version": 2}).status_code == 429


async def test_worker_uses_profile_pinned_when_each_search_is_enqueued(pg_store, tmp_path):
    from qa_job_bot.search_pipeline import SourceBatch
    from qa_job_bot.web_security import WebSettings

    template = SearchSpec.model_validate(search_examples()["items"]["developer"])
    pg_store.save_search_profile(template, 0)
    pg_store.add_source_set([SourceSpec(kind=kind) for kind in AGGREGATORS])
    search, interval = build_search(pg_store)
    assert search.profile.role_keywords == tuple(template.role_keywords) and interval > 0
    built = []

    async def fetch(inputs):
        built.append(tuple(inputs["profile"]["role_keywords"]))
        return [
            SourceBatch(source, [], "2026-11-11T00:00:00+00:00") for source in inputs["sources"]
        ]

    pipeline = type("Pipeline", (), {"fetch": AsyncMock(side_effect=fetch)})()
    worker = Worker(pg_store, WebSettings(tmp_path), search_pipeline=pipeline)
    first = pg_store.enqueue_search("first")
    pg_store.save_search_profile(SearchSpec(role_keywords=["designer"]), 1)
    assert await worker.once()
    assert pg_store.search_run(first["id"])["published"] is False
    second = pg_store.enqueue_search("second")
    assert await worker.once()
    assert pg_store.search_run(second["id"])["published"] is True
    assert built == [tuple(template.role_keywords), ("designer",)]


def test_upgrade_four_backfills_existing_sources_without_changing_configuration(pg_store):
    from alembic import command
    from sqlalchemy import select

    source = pg_store.add_source(SourceSpec(kind="remotive", enabled=False))
    with pg_store.database.engine.begin() as connection:
        command.downgrade(pg_store.database.migration_config(connection), "0003_accounts")
    pg_store.database.migrate()
    assert pg_store.preferences()["sources"]["items"][0] == source
    with pg_store.read() as connection:
        rows = connection.execute(select(s.source_versions)).mappings().all()
    assert len(rows) == 1 and rows[0]["source_id"] == source["id"]
    assert rows[0]["payload"] == {"kind": "remotive", "enabled": False, "config": {}}


async def test_personal_search_pipeline_preserves_profession_and_ownership(pg_store, monkeypatch):
    from qa_job_bot.service import JobSearchService
    from qa_job_bot.source_http import SourceClient

    other = personal_store(pg_store.database, "designer")
    pg_store.save_search_profile(spec(), 0)
    other.save_search_profile(SearchSpec(role_keywords=["product designer"]), 0)
    for store in (pg_store, other):
        store.add_source(SourceSpec(kind="remotive"))
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": role,
                        "title": role,
                        "candidate_required_location": "Worldwide",
                        "company_name": "Acme",
                        "description": "Python product development.",
                        "url": "https://example.test/" + str(index),
                    }
                    for index, role in enumerate(
                        ["Junior Backend Engineer", "Product Designer", "QA Engineer"]
                    )
                ]
            },
        )

    def client(**kwargs):
        return SourceClient(transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr("qa_job_bot.service.SourceClient", client)
    for store, title in [(pg_store, "Junior Backend Engineer"), (other, "Product Designer")]:
        search, _ = build_search(store)
        report = await search.refresh()
        assert [job.title for job in report.jobs] == [title] and not report.errors
        store.publish_search(report.jobs, [])
        assert store.list_jobs("active", 1)["items"][0]["title"] == title
    assert len(requests) == 2
    assert pg_store.list_jobs("active", 1)["items"][0]["title"] == "Junior Backend Engineer"
    assert isinstance(build_search(pg_store)[0], JobSearchService)


def test_sources_reject_bad_edits_without_history_changes_and_profile_uses_api_versions(api_world):
    admin, member, app, _, member_id = api_world
    added = member.post(
        "/api/sources", json={"kind": "lever", "config": {"token": "company"}}
    ).json()
    path = "/api/sources/" + added["id"]
    assert (
        member.patch(path, json={"expected_version": 1, "config": {"token": "../bad"}}).status_code
        == 422
    )
    assert len(member.get(path + "/history").json()["items"]) == 1
    presets = member.get("/api/search-presets").json()["items"]
    first = member.put(
        "/api/search-profile", json={"expected_version": 0, "profile": presets["designer"]}
    )
    assert first.status_code == 200
    stale = member.put(
        "/api/search-profile", json={"expected_version": 0, "profile": presets["developer"]}
    )
    assert stale.status_code == 409
    assert member.get("/api/search-profile").json()["version"] == 1
    store = PostgresStore(app.state.database, member_id)
    with store.write() as connection:
        connection.execute(
            s.user_sources.update()
            .where(s.user_sources.c.id == added["id"])
            .values(payload={"token": "../legacy"})
        )
    assert store.preferences()["sources"]["items"][0]["valid"] is False
    with pytest.raises(StoreError):
        store.search_configuration()
    assert (
        member.patch(path, json={"expected_version": 1, "config": {"token": "fixed"}}).status_code
        == 200
    )
    assert store.preferences()["sources"]["items"][0]["valid"] is True


def test_telegram_titles_do_not_promote_role_mentions_into_job_titles():
    from qa_job_bot.sources import TelegramChannelSource

    source = TelegramChannelSource("test_channel", ("qa",))
    assert (
        source._title(["Backend Engineer", "Collaborate with QA engineers"]) == "Backend Engineer"
    )
    assert source._title(["We are hiring", "Senior QA Engineer"]) == "Senior QA Engineer"
    assert (
        source._location(["Локация: Сербия", "Work from our office"])
        == "Location · Serbia · Локация: Сербия"
    )
