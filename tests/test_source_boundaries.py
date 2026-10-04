import httpx
import pytest

from qa_job_bot.config import Board, SearchProfile
from qa_job_bot.matching import match_job
from qa_job_bot.sources import (
    AshbySource,
    GreenhouseSource,
    HimalayasSource,
    HirifySource,
    LeverSource,
    RemoteComSource,
    RemotiveSource,
    TelegramChannelSource,
    WeWorkRemotelySource,
)
from qa_job_bot.sources.base import JobSource


def response_client(payload: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )


@pytest.mark.parametrize(
    "source",
    [
        RemotiveSource(),
        HimalayasSource(),
        HirifySource(),
        RemoteComSource(),
        AshbySource(Board("ashby", "A", "a")),
        GreenhouseSource(Board("greenhouse", "A", "a")),
        LeverSource(Board("lever", "A", "a")),
        TelegramChannelSource("qa_jobs"),
        WeWorkRemotelySource(),
    ],
)
@pytest.mark.parametrize("status", [429, 500])
async def test_sources_propagate_http_failures(source: JobSource, status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await source.fetch(client)
        assert failure.value.response.status_code == status


@pytest.mark.parametrize(
    "source,payload",
    [
        (RemotiveSource(), {}),
        (HimalayasSource(), {}),
        (HirifySource(), {}),
        (RemoteComSource(), {}),
        (AshbySource(Board("ashby", "A", "a")), {}),
        (GreenhouseSource(Board("greenhouse", "A", "a")), {}),
        (LeverSource(Board("lever", "A", "a")), []),
    ],
)
async def test_empty_source_results(source: JobSource, payload: object) -> None:
    async with response_client(payload) as client:
        assert await source.fetch(client) == []


async def test_abstract_source_requires_a_fetch_implementation() -> None:
    with pytest.raises(TypeError, match="abstract"):
        JobSource()

    class DelegatingSource(JobSource):
        async def fetch(self, client):
            return await super().fetch(client)

    async with response_client({}) as client:
        with pytest.raises(NotImplementedError):
            await DelegatingSource().fetch(client)


async def test_lever_preserves_sections_html_fallback_salary_and_european_host() -> None:
    source = LeverSource(Board("lever", "Acme", "acme", "eu", ("SaaS",)))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "text": "QA Engineer",
                    "description": "<p>Remote role</p>",
                    "categories": {"allLocations": ["Europe", "Serbia"]},
                    "lists": [
                        {"text": "Responsibilities", "content": "<ul><li>Test APIs</li></ul>"},
                        {"text": "Requirements", "content": "<p>English B2</p>"},
                        {},
                    ],
                    "additional": "<p>Candidates must be located in Germany.</p>",
                    "salaryDescription": "<p>Gross compensation</p>",
                    "salaryRange": {
                        "min": 50000,
                        "max": 70000,
                        "currency": "EUR",
                        "interval": "year",
                    },
                    "applyUrl": "https://jobs.example/apply",
                    "workplaceType": "remote",
                },
                {
                    "id": "2",
                    "categories": {"location": "Remote"},
                    "descriptionPlain": "plain",
                    "additionalPlain": "ending",
                    "salaryDescriptionPlain": "salary",
                    "salaryRange": {"min": 0},
                },
                {"id": "3"},
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await source.fetch(client)
    assert seen[0].host == "api.eu.lever.co"
    assert seen[0].params["mode"] == "json"
    assert jobs[0].description == (
        "Remote role\nResponsibilities\nTest APIs\nRequirements\nEnglish B2\n"
        "Candidates must be located in Germany.\nGross compensation"
    )
    assert jobs[0].salary == "50,000–70,000 EUR/year"
    assert jobs[0].location == "Europe, Serbia"
    assert jobs[0].tags == ("SaaS",)
    assert not match_job(jobs[0], SearchProfile()).accepted
    assert jobs[1].description == "plain\nending\nsalary"
    assert jobs[1].remote
    assert jobs[1].salary == ""
    assert (jobs[2].title, jobs[2].location, jobs[2].url, jobs[2].remote) == (
        "Untitled",
        "Not specified",
        "",
        False,
    )


async def test_ashby_unlisted_and_secondary_location_metadata() -> None:
    payload = {
        "jobs": [
            {"id": "hidden", "isListed": False},
            {
                "id": "1",
                "location": "Serbia",
                "secondaryLocations": [{"location": "Serbia"}, {"location": "Europe"}, {}],
                "workplaceType": "Remote",
                "compensation": {"compensationTierSummary": "EUR 5000/month"},
                "applyUrl": "https://jobs.example/1",
                "descriptionPlain": "Requirements: Python",
                "publishedAt": "2026-10-03T10:00:00Z",
                "employmentType": "FullTime",
            },
            {"id": "2"},
        ]
    }
    async with response_client(payload) as client:
        jobs = await AshbySource(Board("ashby", "A", "a")).fetch(client)
    assert len(jobs) == 2
    assert jobs[0].location == "Serbia, Europe"
    assert jobs[0].remote
    assert jobs[0].salary == "EUR 5000/month"
    assert jobs[0].published_at is not None
    assert jobs[1].location == "Not specified"
    assert not jobs[1].remote


@pytest.mark.parametrize("failed_status", [404, None])
async def test_greenhouse_skips_failed_details_and_retains_full_description(
    failed_status: int | None,
) -> None:
    entries = [
        {"id": i, "title": "QA Engineer", "location": {"name": location}, "updated_at": date}
        for i, location, date in [
            (1, "Belgrade", "2026-10-03T10:00:00Z"),
            (2, "Remote", "bad date"),
            (3, "Europe", None),
            (4, "Serbia", None),
        ]
    ]
    entries.append({"id": 5, "title": "Marketing"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": entries}, request=request)
        identifier = request.url.path.rsplit("/", 1)[1]
        if identifier == "4":
            if failed_status is None:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(failed_status, request=request)
        bodies = {
            "1": "<p>Hybrid role</p>",
            "2": "<p>Fully remote.</p>",
            "3": "<p>Office role.</p>",
        }
        return httpx.Response(200, json={"content": bodies[identifier]}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await GreenhouseSource(Board("greenhouse", "A", "a")).fetch(client)
    assert [job.source_id for job in jobs] == ["1", "2", "3"]
    assert [job.workplace_type for job in jobs] == ["hybrid", "remote", ""]
    assert jobs[0].published_at is not None
    assert jobs[1].published_at is None
    assert jobs[2].description == "Office role."


async def test_himalayas_salary_bounds_and_optional_metadata() -> None:
    payload = {
        "jobs": [
            {"guid": "none"},
            {"guid": "low", "minSalary": 0},
            {"guid": "high", "maxSalary": 1000},
            {"guid": "blocked", "locationRestrictions": ["India"]},
            {
                "guid": "full",
                "description": "<p>Full body</p>",
                "excerpt": "truncated",
                "pubDate": 0,
                "categories": ["QA"],
                "parentCategories": ["Engineering"],
            },
        ]
    }
    async with response_client(payload) as client:
        jobs = await HimalayasSource().fetch(client)
    by_id = {job.source_id: job for job in jobs}
    assert set(by_id) == {"none", "low", "high", "full"}
    assert by_id["none"].location == "Worldwide"
    assert by_id["none"].published_at is None
    assert by_id["low"].salary == "0–? "
    assert by_id["high"].salary == "?–1,000 "
    assert by_id["full"].description == "Full body"
    assert by_id["full"].published_at.year == 1970
    assert by_id["full"].tags == ("QA", "Engineering")


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"remote_restrictions": ["usa", "india"]}, "United States, India"),
        ({"allowed_locations": ["global", "india"]}, "Worldwide"),
        ({"remote_type": "global"}, "Worldwide"),
        ({"remote_type": "north_america"}, "North America"),
        (
            {"regions": [{"name_en": "Serbia"}, {"name": "Europe"}, {"code": "ME"}, {}]},
            "Serbia, Europe, ME",
        ),
        ({}, "Remote"),
    ],
)
def test_hirify_location_variants(item: dict, expected: str) -> None:
    assert HirifySource._location(item) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        ({}, ""),
        ({"currency": "EUR"}, ""),
        ({"min": 0, "currency": "EUR", "salary_period": "hour"}, "0–? EUR/hour"),
        ({"max": 100, "currency": "EUR"}, "?–100 EUR"),
    ],
)
def test_hirify_salary_variants(value: dict | None, expected: str) -> None:
    assert HirifySource._salary(value) == expected


async def test_hirify_missing_identity_hidden_company_and_formats() -> None:
    payload = {
        "data": [
            {},
            {"id": 1, "is_scam": True},
            {
                "slug": "qa",
                "company_title": "%hidden",
                "work_format": ["hybrid"],
                "full_description": "Tasks",
                "main_stack": ["Python"],
                "tags": [{}, {"name": "API"}],
            },
            {"id": 2},
        ]
    }
    async with response_client(payload) as client:
        jobs = await HirifySource().fetch(client)
    assert [job.source_id for job in jobs] == ["qa", "2"]
    assert jobs[0].description == "Tasks Python"
    assert jobs[0].company == "Company hidden"
    assert jobs[0].workplace_type == "hybrid"
    assert jobs[0].tags == ("API",)
    assert jobs[1].workplace_type == ""


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"hiring_location": {"type": "global"}}, "Worldwide"),
        (
            {
                "hiring_location": {
                    "included_locations": [
                        {"value": {"name": "Serbia"}},
                        {"value": {"name": "Serbia"}},
                        {"value": {"code": "EU"}},
                        {},
                    ]
                }
            },
            "Serbia, EU",
        ),
        ({"hiring_location": {"timezone": "CET"}}, "CET"),
        ({"hiring_location": {"timezone_range": "UTC+1 to UTC+3"}}, "UTC+1 to UTC+3"),
        ({}, "Remote"),
    ],
)
def test_remote_com_location_variants(item: dict, expected: str) -> None:
    assert RemoteComSource._location(item) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, ""),
        ({"minimum": 0, "currency": {"code": "EUR"}}, "0–? EUR"),
        ({"maximum": 10000, "frequency": "hour"}, "?–100 /hour"),
    ],
)
def test_remote_com_minor_currency_and_missing_bound(value: dict, expected: str) -> None:
    assert RemoteComSource._salary(value) == expected


@pytest.mark.parametrize("failed_status", [500, None])
async def test_remote_com_discards_invalid_identity_non_qa_and_failed_detail(failed_status) -> None:
    listing = {
        "data": {
            "jobs": [
                {},
                {"slug": "sales", "title": "Sales"},
                {"slug": "qa", "title": "QA Engineer", "company_profile": {"slug": "acme"}},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acme/qa"):
            if failed_status is None:
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(failed_status, request=request)
        return httpx.Response(200, json=listing, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await RemoteComSource().fetch(client) == []


async def test_rss_optional_company_location_date_and_guid() -> None:
    xml = (
        "<rss><channel><item><title>QA Engineer</title><country>Serbia</country>"
        "<link>https://jobs.example/1</link></item><item><title>QA</title></item></channel></rss>"
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=xml, request=req))
    async with httpx.AsyncClient(transport=transport) as client:
        jobs = await WeWorkRemotelySource().fetch(client)
    assert jobs[0].company == "Unknown company"
    assert jobs[0].location == "Serbia"
    assert jobs[0].source_id == "https://jobs.example/1"
    assert jobs[0].published_at is None
    assert jobs[1].location == "Remote"


def telegram_html(text: str, identifier: str = "42") -> str:
    return (
        '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
        f'data-post="qa_jobs/{identifier}"><div class="tgme_widget_message_text">{text}'
        '</div></div></div><div class="tgme_channel_info"></div>'
    )


@pytest.mark.parametrize(
    "text,workplace,location",
    [
        ("<p>QA Engineer</p><p>Hybrid Сербия</p>", "hybrid", "Hybrid · Serbia"),
        ("QA Engineer<br>Remote CET", "remote", "Remote CET"),
        ("QA Engineer<br>Office in nowhere", "", "Not specified"),
    ],
)
def test_telegram_optional_metadata_and_workplace(text: str, workplace: str, location: str) -> None:
    jobs, oldest = TelegramChannelSource("qa_jobs")._parse_page(telegram_html(text))
    assert oldest == 42
    job = jobs["qa_jobs/42"]
    assert job.company == "@qa_jobs"
    assert job.workplace_type == workplace
    assert job.location == location
    assert job.salary == ""
    assert job.published_at is None


@pytest.mark.parametrize(
    "lines",
    [
        ["Дайджест вакансий", "QA Engineer"],
        ["QA", "Тестировщик", "Requirements: QA"],
        ["QA Engineer " * 30],
        ["Backend Engineer"],
    ],
)
def test_telegram_rejects_digest_tags_requirements_and_oversize_titles(lines: list[str]) -> None:
    assert TelegramChannelSource("qa_jobs")._title(lines) == ""


async def test_telegram_empty_and_malformed_pages_do_not_request_history() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200,
            text='<div class="tgme_widget_message_wrap">'
            '<div>QA Engineer</div></div><div class="tgme_channel_info"></div>',
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await TelegramChannelSource("qa_jobs").fetch(client) == []
    assert len(seen) == 1


async def test_source_malformed_json_propagates_without_inventing_jobs() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="broken", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError):
            await RemotiveSource().fetch(client)
