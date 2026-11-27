from datetime import date

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_accounts import api_world as api_world
from test_personal_search_runs import batch, configure, started

from qa_job_bot import pg_schema as s
from qa_job_bot.geography import find_places, mentioned_places
from qa_job_bot.job_details import extract_details
from qa_job_bot.job_language import language_display, matches_working_languages
from qa_job_bot.matching import match_job
from qa_job_bot.models import Job
from qa_job_bot.openai_config import OpenAISettings
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.posting_details import (
    PublicPostingSearch,
    fetch_posting,
    remote_com_original,
    remote_com_posting_key,
    same_posting,
)
from qa_job_bot.posting_summary import posting_summary
from qa_job_bot.search_models import SearchSpec
from qa_job_bot.source_http import SourceClient
from qa_job_bot.source_import import csv_rows, sheets_csv, sheets_download_host
from qa_job_bot.source_links import posting_link, source_link
from qa_job_bot.web_store import StoreError
from qa_job_bot.work_time import display_work_time

ORIGINAL = "https://job-boards.greenhouse.io/example/jobs/123"
REMOTE_LISTING = "https://remote.com/jobs/example-labs/engineer-123"
ORIGINAL_TEXT = (
    "Build product features using Python and maintain reliable application services. "
    "Own the event pipeline for school enrollment and automate its reconciliation tests."
)


def vacancy(**fields):
    return Job(
        **{
            "source": "Fixture",
            "source_id": "123",
            "title": "Backend Engineer",
            "company": "Example",
            "location": "Worldwide",
            "remote": True,
            "url": ORIGINAL,
            **fields,
        }
    )


def criteria(**fields):
    return SearchSpec.model_validate({"role_keywords": ["backend engineer"], **fields})


@pytest.mark.parametrize(
    ("job_data", "settings", "status", "reason"),
    [
        ({"location": "Europe only"}, {}, "unknown", "geography_unconfirmed"),
        (
            {"location": "Remote — EU only"},
            {"remote_place_ids": ["city:rs-belgrade"]},
            "blocked",
            "eu_only",
        ),
        ({"location": "Remote — EU only"}, {}, "blocked", "eu_only"),
        (
            {"location": "Remote — EU only"},
            {"remote_place_ids": ["country:DE"]},
            "allowed",
            "hiring_location_match",
        ),
        (
            {"location": "Remote — Germany only"},
            {"remote_place_ids": ["region:eu"]},
            "allowed",
            "hiring_location_match",
        ),
        (
            {"location": "Remote — US only"},
            {"remote_place_ids": ["region:eu"]},
            "blocked",
            "hiring_location_mismatch",
        ),
        (
            {"location": "Remote — Norway only"},
            {"remote_place_ids": ["region:eea"]},
            "allowed",
            "hiring_location_match",
        ),
        (
            {"description": "Candidates must reside in Serbia or Republic of Georgia."},
            {},
            "allowed",
            "hiring_location_match",
        ),
        ({"location": "US only"}, {}, "blocked", "hiring_location_mismatch"),
        (
            {"description": "Fully remote. Remote from the United States."},
            {},
            "blocked",
            "hiring_location_mismatch",
        ),
        ({"location": "Worldwide, except Georgia"}, {}, "blocked", "employer_exclusion"),
        (
            {"description": "Where you can work\n- Chile\n- Mexico\n- Colombia"},
            {},
            "blocked",
            "hiring_location_mismatch",
        ),
        (
            {"description": "We have a US-based team that only works asynchronously."},
            {},
            "allowed",
            "hiring_location_match",
        ),
        (
            {"description": "Working hours required: 9 am–5 pm US Eastern."},
            {"availability": {"zone": "Asia/Tbilisi", "start": "09:00", "end": "17:00"}},
            "blocked",
            "working_hours_conflict",
        ),
        (
            {
                "description": "Flexible working hours. You can start at 3 PM. "
                "Must overlap with 9 am–5 pm US Eastern."
            },
            {"availability": {"zone": "Asia/Tbilisi", "start": "09:00", "end": "17:00"}},
            "blocked",
            "working_hours_conflict",
        ),
        (
            {"location": "Berlin, Germany", "workplace_type": "hybrid"},
            {"hybrid_place_ids": ["city:de-berlin"]},
            "allowed",
            "office_match",
        ),
        (
            {"location": "Berlin, Germany", "workplace_type": "on-site"},
            {"office_place_ids": ["city:ge-tbilisi"]},
            "not_remote",
            "office_mismatch",
        ),
        ({"location": "Remote"}, {}, "unknown", "geography_unconfirmed"),
        (
            {"location": "Chile, Remote"},
            {"excluded_place_ids": ["region:latam"]},
            "blocked",
            "preference_exclusion",
        ),
    ],
)
def test_geography_decision_and_explanation(job_data, settings, status, reason):
    result = match_job(
        vacancy(**job_data),
        criteria(
            **{
                "remote_place_ids": ["city:ge-tbilisi"],
                "preferred_place_ids": ["region:europe"],
                **settings,
            }
        ).runtime(),
        day=date(2026, 11, 26),
    )
    assert result.location_status == status
    assert result.location_code == reason


def test_place_choices_disambiguate_and_never_rewrite_old_values():
    assert "country:DE" in {item["id"] for item in find_places("герм", ("country",))["items"]}
    for term in ("me", "ме"):
        assert "MENA · Ближний Восток и Северная Африка" in {
            item["label"] for item in find_places(term)["items"]
        }
    georgia = {item["id"] for item in find_places("Georgia")["items"]}
    assert {"country:GE", "state:us-ga"} <= georgia
    assert len(find_places("berlin", ("city",))["items"]) == 2
    suggested = find_places("Tbilsi", ("city",))
    assert suggested["corrected"] and suggested["items"][0]["label"] == "Тбилиси, Грузия"
    assert mentioned_places("Berlin, Germany") == {"city:de-berlin", "country:DE"}
    assert not find_places("qwertyunrecognizable")["items"]
    assert criteria(candidate_locations=["old place"]).candidate_locations == ["old place"]


@pytest.mark.parametrize(
    "settings",
    [
        {"remote_place_ids": ["region:world"]},
        {"hybrid_place_ids": ["region:europe"]},
        {"office_place_ids": ["city:made-up"]},
        {"preferred_place_ids": ["country:RS"], "excluded_place_ids": ["region:europe"]},
        {"availability": {"zone": "unknown", "start": "09:00", "end": "10:00"}},
        {"availability": {"zone": "America/New_York", "start": "09:00", "end": "09:00"}},
    ],
)
def test_search_profile_rejects_invalid_geo_and_time(settings):
    with pytest.raises(ValidationError):
        criteria(**settings)


def test_work_time_uses_reference_date_and_keeps_uncertain_evidence():
    availability = {"zone": "Europe/Belgrade", "start": "08:00", "end": "21:00"}
    line = "Required working hours: 9 AM to 5 PM US Eastern."
    march = display_work_time(line, availability, date(2026, 3, 12))
    november = display_work_time(line, availability, date(2026, 11, 12))
    assert march["local_start"][11:16] == "14:00"
    assert november["local_start"][11:16] == "15:00"
    assert not march["needs_review"] and march["evidence"] == line
    assert display_work_time("You can start at 3 PM", availability, date(2026, 3, 12))[
        "needs_review"
    ]
    assert display_work_time("Join our US-based team.", availability, date(2026, 3, 12)) is None


def test_language_requirement_preserves_joint_and_alternative_meanings():
    text = "Working languages: English.\nRequirements: Russian and English"
    evidence = language_display(text, "Example")
    assert [item["kind"] for item in evidence] == ["team", "required"]
    assert evidence[1]["alternatives"] == [["en", "ru"]]
    assert not matches_working_languages(text, "Example", ["ru"])
    assert matches_working_languages(text, "Example", ["ru", "en"])
    assert matches_working_languages("Requirements: Russian or English", "Example", ["en"])
    assert not matches_working_languages("Requirements: Russian", "Example", ["en"])
    assert (
        language_display("We are hiring to build more reliable features.", "Example")[0]["kind"]
        == "inferred"
    )


def test_named_revisions_are_immutable_and_activation_protects_against_aba(pg_store):
    configure(pg_store)
    initial = pg_store.preferences()["search_profile"]
    old_run = started(pg_store)
    spec = criteria(skill_keywords=["python"])
    first = pg_store.save_search_profile(
        spec,
        initial["version"],
        expected_editor_version=initial["editor_version"],
        variant_id=initial["variant_id"],
    )
    assert first["variant_id"] == initial["variant_id"]
    repeated = pg_store.save_search_profile(
        spec,
        first["version"],
        expected_editor_version=first["editor_version"],
        variant_id=first["variant_id"],
    )
    assert repeated["id"] == first["id"]
    another = pg_store.save_search_profile(
        spec,
        first["version"],
        expected_editor_version=first["editor_version"],
        create=True,
        name="Название варианта",
    )
    assert another["id"] == first["id"] and another["saved_variant_id"] != first["variant_id"]
    activated = pg_store.change_profile_variant(
        another["saved_version"], "activate", another["editor_version"]
    )
    assert activated["name"] == "Название варианта"
    activated = pg_store.change_profile_variant(
        initial["version"], "activate", activated["editor_version"]
    )
    assert activated["id"] == initial["id"] and activated["generation"] > initial["generation"]
    with pytest.raises(StoreError, match="другой вкладке"):
        pg_store.save_search_profile(
            spec, initial["version"], expected_editor_version=initial["editor_version"]
        )
    with pytest.raises(StoreError, match="выберите другой"):
        pg_store.change_profile_variant(initial["version"], "delete", activated["editor_version"])
    renamed = pg_store.change_profile_variant(
        another["saved_version"], "rename", activated["editor_version"], "Моя подборка"
    )
    assert renamed["generation"] == activated["generation"]
    deleted = pg_store.change_profile_variant(
        another["saved_version"], "delete", renamed["editor_version"]
    )
    assert deleted["id"] == initial["id"]
    assert pg_store.complete_search(old_run, "worker", [batch(old_run, [vacancy()])])
    assert not pg_store.search_run(old_run["id"])["published"]
    assert pg_store.list_jobs("active", 1, run_id=old_run["id"])["total"] == 1
    assert pg_store.search_profile_history()[-1]["deleted"]


def test_settings_and_import_api_exposes_validation_and_scopes(api_world):
    admin, member, _, _, _ = api_world
    invalid = member.put(
        "/api/search-profile",
        json={
            "expected_version": 0,
            "profile": {"role_keywords": [], "min_score": 101},
        },
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "validation"
    assert {"role_keywords", "min_score"} <= invalid.json()["fields"].keys()
    assert (
        member.get("/api/places", params={"q": "tbilisi"}).json()["items"][0]["id"]
        == "city:ge-tbilisi"
    )
    old = member.get(
        "/api/places/legacy", params=[("value", "tbilisi"), ("value", "qwerty")]
    ).json()
    assert old["tbilisi"][0]["id"] == "city:ge-tbilisi" and not old["qwerty"]
    builtin = member.post("/api/sources", json={"kind": "remotive", "enabled": False}).json()
    assert (
        member.request(
            "DELETE", f"/api/sources/{builtin['id']}", json={"expected_version": 1}
        ).status_code
        == 409
    )
    assert (
        member.patch(
            f"/api/sources/{builtin['id']}", json={"expected_version": 1, "config": {}}
        ).status_code
        == 422
    )
    assert len(member.post("/api/sources/recommended").json()["items"]) == 4
    assert not member.get("/api/sources").json()["items"][0]["enabled"]
    csv_data = (
        "Компания,Ссылка,Теги\n"
        "Example,https://jobs.eu.lever.co/example,design\n"
        "Telegram,https://t.me/public_jobs,remote\n"
        "Duplicate,@PUBLIC_JOBS,remote\n"
        "Missing,,\n"
        "Formula,https://jobs.ashbyhq.com/example,=1\n"
    ).encode()
    result = member.post("/api/sources/import/file", files={"file": ("companies.csv", csv_data)})
    assert result.status_code == 200
    rows = result.json()["items"]
    assert [(item["row"], item["status"]) for item in rows] == [
        (2, "new"),
        (3, "new"),
        (4, "duplicate"),
        (5, "invalid"),
        (6, "invalid"),
    ]
    body = {"items": [rows[1]["spec"]]}
    confirmed = member.post("/api/sources/import", json=body).json()
    assert confirmed["items"][0]["config"]["tags"] == ["remote"]
    assert not member.post("/api/sources/import", json=body).json()["items"]
    assert member.post("/api/sources/resolve", json={"url": "@public_jobs"}).json()["existing"]
    assert not admin.post("/api/sources/resolve", json={"url": "@public_jobs"}).json()["existing"]
    assert (
        member.post("/api/sources/resolve", json={"url": "https://127.0.0.1/private"}).status_code
        == 422
    )


def test_csv_limits_and_normalization():
    assert source_link("https://jobs.eu.lever.co/shop/")[1]["region"] == "eu"
    assert source_link("https://t.me/s/public_jobs/123")[1] == {"handle": "public_jobs"}
    for body in (b"", b"company,url\n\xff", b"bad\nvalue", b"company,url\n" * 102):
        with pytest.raises(StoreError):
            csv_rows(body, set())


@pytest.mark.parametrize("status", [200, 302])
async def test_spreadsheet_export_restricts_redirects(status):
    def handler(request):
        assert (
            str(request.url)
            == "https://docs.google.com/spreadsheets/d/12345678/export?format=csv&gid=7"
        )
        if status == 200:
            return httpx.Response(200, headers={"Content-Type": "text/csv"}, text="company,url\n")
        return httpx.Response(302, headers={"Location": "https://invalid.test/private"})

    def factory():
        return SourceClient(transport=httpx.MockTransport(handler))

    link = "https://docs.google.com/spreadsheets/d/12345678/edit#gid=7"
    if status == 200:
        assert await sheets_csv(link, factory) == b"company,url\n"
    else:
        with pytest.raises(StoreError, match="неподдерживаемый"):
            await sheets_csv(link, factory)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("doc-a1-b2-sheets.googleusercontent.com", True),
        ("doc--sheets.googleusercontent.com", False),
        ("doc-a.b-sheets.googleusercontent.com", False),
        ("doc-" * 256 + "sheets.googleusercontent.com.invalid.test", False),
    ],
)
def test_spreadsheet_download_hostname(host, allowed):
    assert sheets_download_host(host) is allowed


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, "open"),
        (404, "closed"),
        (403, "unconfirmed"),
        (302, "unconfirmed"),
        (429, "unconfirmed"),
    ],
)
async def test_ats_contracts_keep_declared_and_unconfirmed_states_separate(status, expected):
    def handler(request):
        assert request.url.path == "/v1/boards/example/jobs/123"
        return httpx.Response(
            status,
            json={
                "id": 123,
                "title": "Backend Engineer",
                "company_name": "Example",
                "content": f"<p>{ORIGINAL_TEXT}</p><script>injected text</script>",
            },
        )

    result = await fetch_posting(
        ORIGINAL, lambda: SourceClient(transport=httpx.MockTransport(handler))
    )
    assert result.status == expected
    if status == 200:
        assert result.job["description"] == ORIGINAL_TEXT
        assert result.job["description_quality"] == "full"
        assert same_posting(
            {"title": "Backend Engineer", "company": "Example", "url": ORIGINAL}, result.job
        )
        assert not same_posting(
            {"title": "Backend Engineer", "company": "Other", "source_id": "123"}, result.job
        )
        public_job = {
            "title": "Backend Engineer",
            "company": "Example",
            "url": "https://remotive.com/remote-jobs/example-98745",
            "source_id": "98745",
            "description": ORIGINAL_TEXT,
        }
        assert same_posting(public_job, result.job)
        assert not same_posting({**public_job, "description": "Engineering position."}, result.job)
        assert not same_posting({**public_job, "url": ORIGINAL.replace("/123", "/456")}, result.job)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (REMOTE_LISTING, ("example-labs", "engineer-123")),
        (REMOTE_LISTING + "?ref=search", ("example-labs", "engineer-123")),
        (REMOTE_LISTING.replace("remote.com", "remote.com.example.net"), None),
        (REMOTE_LISTING.replace("engineer-123", "%2Fengineer-123"), None),
        (REMOTE_LISTING.replace("/jobs/", "/companies/"), None),
        ("https://remote.com/jobs/example-labs", None),
        (REMOTE_LISTING + "!", None),
    ],
)
def test_remote_posting_key_matches_the_public_listing_route(url, expected):
    assert remote_com_posting_key(url) == expected


@pytest.mark.parametrize(
    ("status", "company", "slug", "url", "found"),
    [
        (200, "example-labs", "engineer-123", ORIGINAL, True),
        (200, "different", "engineer-123", ORIGINAL, False),
        (200, "example-labs", "engineer-124", ORIGINAL, False),
        (200, "example-labs", "engineer-123", "https://example.net", False),
        (200, "example-labs", "engineer-123", None, False),
        (429, "example-labs", "engineer-123", ORIGINAL, False),
    ],
)
async def test_remote_original_checks_both_identifiers_and_requires_an_ats_link(
    status, company, slug, url, found
):
    def handler(request):
        assert request.url.host == "talent-api.remote.com"
        assert request.url.path == "/api/v1/public/jobs/example-labs/engineer-123"
        return httpx.Response(
            status,
            json={"data": {"company_profile": {"slug": company}, "slug": slug, "apply_url": url}},
        )

    result = await remote_com_original(
        remote_com_posting_key(REMOTE_LISTING),
        lambda: SourceClient(transport=httpx.MockTransport(handler)),
    )
    assert result == (posting_link(ORIGINAL) if found else None)


def test_manual_add_preserves_owner_context_url_deduplication_and_revisions(api_world):
    admin, member, app, _, member_id = api_world
    store = PostgresStore(app.state.database, member_id)
    data = {
        "title": "Backend Engineer",
        "company": "Manual secret company",
        "url": "https://boards.greenhouse.io/example/jobs/123?tracking=source",
        "description": f"<div>{ORIGINAL_TEXT}</div><script>fake approval</script>",
        "request_id": "manual-1",
    }
    saved = member.post("/api/manual-jobs", json=data)
    assert saved.status_code == 201
    job_id = saved.json()["id"]
    assert not saved.json()["existing"]
    assert member.post("/api/manual-jobs", json=data).json() == {"id": job_id, "existing": True}
    assert member.post(
        "/api/manual-jobs", json={**data, "url": ORIGINAL, "request_id": "manual-2"}
    ).json()["existing"]
    assert (
        member.post(
            "/api/manual-jobs", json={**data, "description": ORIGINAL_TEXT + " New"}
        ).status_code
        == 409
    )
    assert member.get("/api/jobs").json()["total"] == 0
    entry = member.get("/api/jobs", params={"tab": "manual"}).json()["items"][0]
    assert entry["description"] == ORIGINAL_TEXT and entry["url"] == ORIGINAL
    assert admin.get("/api/jobs", params={"tab": "manual"}).json()["total"] == 0
    assert (
        admin.post(f"/api/jobs/{job_id}/check-posting", json={"request_id": "other"}).status_code
        == 404
    )
    assert (
        member.post(
            f"/api/jobs/{job_id}/documents",
            json={
                "kind": "cv",
                "language": "en",
                "request_id": "needs-confirmed-template",
            },
        ).status_code
        == 409
    )
    with store.read() as connection:
        public = connection.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().one()
        assert not public["url"] and not public["company"]
        frozen, first = store.selected_document_job(connection, job_id)
    edit = {key: value for key, value in data.items() if key != "request_id"}
    edit.update(description=ORIGINAL_TEXT + " New description.", expected_version=1)
    assert member.patch(f"/api/manual-jobs/{job_id}", json=edit).status_code == 200
    assert member.patch(f"/api/manual-jobs/{job_id}", json=edit).status_code == 409
    with store.read() as connection:
        current, second = store.selected_document_job(connection, job_id)
    assert first["content_hash"] != second["content_hash"]
    assert frozen["description"] != current["description"]
    assert posting_link(entry["url"]).posting == "123"


async def test_web_search_uses_only_public_fields_and_url_annotations():
    settings = OpenAISettings("sk-" + "x" * 25, "agent_A123456")
    events = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert b"SECRET" not in request.content
        assert b'"store":false' in request.content
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 47},
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Not used",
                                "annotations": [{"type": "url_citation", "url": ORIGINAL}],
                            }
                        ],
                    }
                ],
            },
        )

    search = PublicPostingSearch(settings, transport=httpx.MockTransport(handler))
    links = await search.find(
        {"title": "Example job", "description": "SECRET"},
        lambda event, data: events.append((event, data)),
    )
    assert links == [ORIGINAL]
    assert [event for event, _ in events] == ["sent", "usage"]


def test_summary_has_original_evidence_and_skips_missing_fields():
    description = (
        "Industry: Education\nCompany size: 50–200\nRole focus: Backend services\n"
        "Requirements:\nAt least 4 years of experience with Python"
    )
    items = posting_summary(description, extract_details(description), ORIGINAL)
    assert {item["field"] for item in items} >= {
        "industry",
        "company_size",
        "experience",
        "role_focus",
        "stack",
    }
    assert not any(item["field"] == "founded" for item in items)
    assert all(item["url"] == ORIGINAL and item["evidence"] in description for item in items)


def test_summary_extracts_company_and_role_from_prose_without_labelled_fields():
    description = (
        "About us\nWe are a fintech company founded in 2018 with 100 employees.\n"
        "About the role\nYou will design onboarding for business clients."
    )
    items = posting_summary(description, extract_details(description), ORIGINAL)
    fields = {item["field"]: item["value"] for item in items}
    assert fields["industry"] == "fintech"
    assert fields["company_size"] == "100 employees"
    assert fields["founded"] == "2018"
    assert "onboarding" in fields["role_focus"]
    assert "experience" not in fields
    assert all(item["evidence"] in description for item in items)


def test_work_time_prefers_explicit_hours_and_preserves_a_standalone_zone():
    day = date(2026, 11, 26)
    required = display_work_time(
        "Flexible working hours. Must overlap with 9 am–5 pm US Eastern.", None, day
    )
    assert required["mandatory"] and required["start"] == "09:00" and not required["needs_review"]
    zone_only = display_work_time("Fully remote, CET.", None, day)
    assert zone_only["zone"] == "UTC+01:00" and zone_only["needs_review"]


def test_automatic_posting_checks_are_bounded_and_require_a_resolvable_link(pg_store):
    configure(pg_store)
    search = started(pg_store)
    jobs = [vacancy(source_id="remote", url=REMOTE_LISTING)]
    jobs.extend(vacancy(source_id=str(index), url=f"{ORIGINAL}{index}") for index in range(12))
    jobs.append(
        vacancy(source_id="aggregator", url="https://remotive.com/remote-jobs/example-98745")
    )
    assert pg_store.complete_search(search, "worker", [batch(search, jobs)])
    checks = [task for task in pg_store.tasks() if task["kind"] == "posting_check"]
    assert len(checks) == 10
    assert all(
        pg_store.task(task["id"], internal=True)["payload"]["posting_job"]["source_id"]
        != "aggregator"
        for task in checks
    )


@pytest.mark.parametrize("status", [200, 404, 503])
async def test_remote_original_enriches_or_hides_the_live_card(pg_store, tmp_path, status):
    from qa_job_bot.search_pipeline import PersonalSearchPipeline
    from qa_job_bot.web_security import WebSettings
    from qa_job_bot.worker import Worker

    configure(pg_store)
    search = started(pg_store)
    job = vacancy(
        url=REMOTE_LISTING, description="Build with Python", description_quality="fragment"
    )
    assert pg_store.complete_search(search, "worker", [batch(search, [job])])
    item = pg_store.list_jobs("active", 1)["items"][0]
    checked_hosts = []

    def handler(request):
        checked_hosts.append(request.url.host)
        if request.url.host == "talent-api.remote.com":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "company_profile": {"slug": "example-labs"},
                        "slug": "engineer-123",
                        "apply_url": ORIGINAL,
                    }
                },
            )
        assert str(request.url).endswith("/example/jobs/123")
        return httpx.Response(
            status, json={"id": 123, "title": "Backend Engineer", "content": ORIGINAL_TEXT}
        )

    def factory():
        return SourceClient(transport=httpx.MockTransport(handler))

    worker = Worker(
        pg_store, WebSettings(tmp_path), search_pipeline=PersonalSearchPipeline(factory)
    )
    task = pg_store.enqueue_posting_check(item["id"], "read-original")["task"]
    assert await worker.once()
    assert checked_hosts == ["talent-api.remote.com", "boards-api.greenhouse.io"]
    live = pg_store.list_jobs("active", 1)["items"]
    history = pg_store.list_jobs("active", 1, run_id=search["id"])["items"]
    if status == 404:
        assert not live and history[0]["posting"]["status"] == "closed"
    elif status == 200:
        assert live[0]["url"] == ORIGINAL and live[0]["description"] == ORIGINAL_TEXT
    else:
        assert live[0]["description"] == "Build with Python"
        assert live[0]["posting"]["status"] == "unconfirmed"
    assert pg_store.task(task["id"])["status"] == "ready"


async def test_posting_worker_keeps_history_and_applied_during_close_and_reopen(
    pg_store,
    tmp_path,
    monkeypatch,
):
    from datetime import timedelta

    from qa_job_bot import pg_manual_jobs
    from qa_job_bot.search_pipeline import PersonalSearchPipeline
    from qa_job_bot.web_security import WebSettings
    from qa_job_bot.worker import Worker

    configure(pg_store)
    search = started(pg_store)
    assert pg_store.complete_search(search, "worker", [batch(search, [vacancy()])])
    item = pg_store.list_jobs("active", 1)["items"][0]
    statuses = iter((404, 403, 200))

    def handler(request):
        return httpx.Response(
            next(statuses),
            json={
                "id": 123,
                "title": "Backend Engineer",
                "company_name": "Example",
                "content": ORIGINAL_TEXT,
            },
        )

    def factory():
        return SourceClient(transport=httpx.MockTransport(handler))

    worker = Worker(
        pg_store, WebSettings(tmp_path), search_pipeline=PersonalSearchPipeline(factory)
    )
    checks = [task for task in pg_store.tasks() if task["kind"] == "posting_check"]
    assert len(checks) == 1 and checks[0]["status"] == "queued"
    first = checks[0]
    assert pg_store.enqueue_posting_check(item["id"], "request-retry")["task"]["id"] == first["id"]
    assert await worker.once()
    result = pg_store.task(first["id"])
    assert result["status"] == "ready" and result["result"]["posting"]["status"] == "closed"
    assert pg_store.list_jobs("active", 1)["items"] == []
    assert pg_store.list_jobs("active", 1, run_id=search["id"])["total"] == 1
    assert pg_store.enqueue_posting_check(item["id"], "request-2")["posting"]["status"] == "closed"
    pg_store.change_state(item["id"], {"applied": True})
    assert pg_store.list_jobs("applied", 1)["items"][0]["posting"]["status"] == "closed"
    pg_store.change_state(item["id"], {"archived": True})
    assert pg_store.list_jobs("archive", 1)["items"][0]["posting"]["status"] == "closed"
    later = pg_manual_jobs.utcnow() + timedelta(minutes=6)
    monkeypatch.setattr(pg_manual_jobs, "utcnow", lambda: later)
    second = pg_store.enqueue_posting_check(item["id"], "request-3")["task"]
    assert second["id"] != first["id"]
    assert await worker.once()
    assert pg_store.task(second["id"])["result"]["posting"]["status"] == "unconfirmed"
    assert pg_store.list_jobs("archive", 1)["items"][0]["posting"]["status"] == "closed"
    pg_store.change_state(item["id"], {"applied": False, "archived": False})
    assert pg_store.list_jobs("active", 1)["items"] == []
    monkeypatch.setattr(pg_manual_jobs, "utcnow", lambda: later + timedelta(minutes=6))
    third = pg_store.enqueue_posting_check(item["id"], "request-4")["task"]
    assert third["id"] != second["id"]
    assert await worker.once()
    assert pg_store.task(third["id"])["result"]["posting"]["status"] == "open"
    assert pg_store.list_jobs("active", 1)["items"][0]["description"] == ORIGINAL_TEXT
