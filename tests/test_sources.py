import httpx

from qa_job_bot.config import Board
from qa_job_bot.sources.ashby import AshbySource
from qa_job_bot.sources.greenhouse import GreenhouseSource
from qa_job_bot.sources.himalayas import HimalayasSource
from qa_job_bot.sources.hirify import HirifySource
from qa_job_bot.sources.lever import LeverSource
from qa_job_bot.sources.remote_com import RemoteComSource
from qa_job_bot.sources.remotive import RemotiveSource
from qa_job_bot.sources.telegram_channel import TelegramChannelSource
from qa_job_bot.sources.weworkremotely import WeWorkRemotelySource


async def client_for(payload: object) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_remotive_parser() -> None:
    client = await client_for(
        {
            "jobs": [
                {
                    "id": 1,
                    "title": "QA Lead",
                    "company_name": "Acme",
                    "candidate_required_location": "Europe",
                    "url": "https://example.com/1",
                    "description": "<p>Playwright</p>",
                    "publication_date": "2026-09-01T12:00:00Z",
                    "job_type": "full_time",
                    "tags": ["QA"],
                }
            ]
        }
    )
    async with client:
        jobs = await RemotiveSource().fetch(client)
    assert jobs[0].remote is True
    assert jobs[0].description == "Playwright"


async def test_greenhouse_parser() -> None:
    source = GreenhouseSource(Board("greenhouse", "Acme", "acme"))
    client = await client_for(
        {
            "jobs": [
                {
                    "id": 2,
                    "title": "SDET",
                    "location": {"name": "Remote - Europe"},
                    "absolute_url": "https://example.com/2",
                    "content": "<p>Automation</p>",
                }
            ]
        }
    )
    async with client:
        jobs = await source.fetch(client)
    assert jobs[0].company == "Acme"
    assert jobs[0].remote is True


async def test_lever_parser() -> None:
    source = LeverSource(Board("lever", "Acme", "acme"))
    client = await client_for(
        [
            {
                "id": "3",
                "text": "QA Manager",
                "categories": {"location": "Europe", "commitment": "Full-time"},
                "hostedUrl": "https://example.com/3",
                "descriptionPlain": "Quality strategy",
                "workplaceType": "remote",
            }
        ]
    )
    async with client:
        jobs = await source.fetch(client)
    assert jobs[0].employment_type == "Full-time"
    assert jobs[0].remote is True


async def test_ashby_parser() -> None:
    source = AshbySource(Board("ashby", "Acme", "acme"))
    client = await client_for(
        {
            "jobs": [
                {
                    "id": "4",
                    "title": "Quality Engineering Lead",
                    "location": "Serbia",
                    "isListed": True,
                    "isRemote": True,
                    "jobUrl": "https://example.com/4",
                    "descriptionHtml": "<p>QA strategy</p>",
                }
            ]
        }
    )
    async with client:
        jobs = await source.fetch(client)
    assert jobs[0].location == "Serbia"
    assert jobs[0].description == "QA strategy"


async def test_himalayas_parser_and_query_deduplication() -> None:
    assert "manual qa" in HimalayasSource.queries
    assert "full stack qa" in HimalayasSource.queries
    client = await client_for(
        {
            "jobs": [
                {
                    "guid": "https://himalayas.app/jobs/1",
                    "title": "Senior SDET",
                    "companyName": "Acme",
                    "locationRestrictions": ["United States", "Canada", "Poland"],
                    "applicationLink": "https://himalayas.app/jobs/1",
                    "description": "<p>Test automation</p>",
                    "pubDate": 1_788_000_000,
                    "minSalary": 100_000,
                    "maxSalary": 130_000,
                    "currency": "USD",
                    "salaryPeriod": "annual",
                    "categories": ["SDET"],
                }
            ]
        }
    )
    async with client:
        jobs = await HimalayasSource().fetch(client)
    assert len(jobs) == 1
    assert jobs[0].location == "Poland"
    assert jobs[0].salary == "100,000–130,000 USD/annual"


async def test_himalayas_excludes_other_continents_only() -> None:
    client = await client_for(
        {
            "jobs": [
                {
                    "guid": "https://himalayas.app/jobs/other-region",
                    "title": "QA Lead",
                    "companyName": "Acme",
                    "locationRestrictions": ["Brazil", "India", "Australia"],
                    "applicationLink": "https://himalayas.app/jobs/other-region",
                    "description": "Remote QA",
                }
            ]
        }
    )
    async with client:
        jobs = await HimalayasSource().fetch(client)
    assert jobs == []


async def test_remote_com_parser_uses_public_api_and_details() -> None:
    assert "manual qa" in RemoteComSource.queries
    assert "full stack qa" in RemoteComSource.queries
    listing = {
        "data": {
            "jobs": [
                {
                    "title": "Senior QA Engineer",
                    "slug": "senior-qa-j1",
                    "company_profile": {"name": "Acme", "slug": "acme-c1"},
                    "hiring_location": {
                        "type": "location",
                        "included_locations": [{"value": {"name": "Poland", "code": "POL"}}],
                    },
                    "workplace_location": {"type": "remote"},
                    "published_at": "2026-09-17T10:00:00Z",
                    "employment_type": "full_time",
                    "compensation": {
                        "minimum": 8_000_000,
                        "maximum": 10_000_000,
                        "currency": {"code": "EUR"},
                        "frequency": "yearly",
                    },
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acme-c1/senior-qa-j1"):
            return httpx.Response(
                200,
                json={"data": {"description": "<p>Playwright and API testing</p>"}},
                request=request,
            )
        return httpx.Response(200, json=listing, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await RemoteComSource().fetch(client)
    assert len(jobs) == 1
    assert jobs[0].location == "Poland"
    assert jobs[0].description == "Playwright and API testing"
    assert jobs[0].salary == "80,000–100,000 EUR/yearly"
    assert jobs[0].workplace_type == "remote"


async def test_hirify_parser_filters_flagged_jobs() -> None:
    payload = {
        "data": [
            {
                "id": 10,
                "slug": "10-qa-lead",
                "title": "QA Lead",
                "company_title": "Acme",
                "work_format": ["remote"],
                "remote_type": "europe",
                "remote_restrictions": ["Poland", "India"],
                "work_type": "fulltime",
                "tldr": "Fintech test automation",
                "updated_at": "2026-09-17T10:00:00Z",
                "tags": [{"name": "playwright"}],
                "salary": {
                    "min": 4_000,
                    "max": 5_000,
                    "currency": "EUR",
                    "salary_period": "month",
                },
            },
            {
                "id": 11,
                "slug": "11-suspicious-qa",
                "title": "Senior QA Engineer",
                "work_format": ["remote"],
                "remote_type": "global",
                "is_potential_scam": True,
            },
        ]
    }
    client = await client_for(payload)
    async with client:
        jobs = await HirifySource().fetch(client)
    assert len(jobs) == 1
    assert jobs[0].location == "Poland"
    assert jobs[0].salary == "4,000–5,000 EUR/month"
    assert jobs[0].tags == ("playwright",)


async def test_public_telegram_channel_parser() -> None:
    html = """
    <html><body>
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message" data-post="qa_channel/42">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          <b>Senior QA Automation Engineer</b><br>
          Компания: Acme<br>
          Формат: удалённо, Сербия<br>
          Playwright, Python, fintech<br>
          Зарплата: 4 000–5 000 EUR
        </div>
        <a class="tgme_widget_message_date" href="https://t.me/qa_channel/42">
          <time datetime="2026-09-18T10:00:00+00:00"></time>
        </a>
      </div>
    </div>
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message" data-post="qa_channel/41">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          <b>Senior Backend Engineer</b><br>
          Remote, Serbia<br>
          Задачи: взаимодействовать с QA и улучшать качество продукта
        </div>
        <time datetime="2026-09-17T10:00:00+00:00"></time>
      </div>
    </div>
    <div class="tgme_channel_info"></div>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await TelegramChannelSource("qa_channel").fetch(client)
    assert len(jobs) == 1
    assert jobs[0].source_id == "qa_channel/42"
    assert jobs[0].company == "Acme"
    assert jobs[0].location == "Remote · Serbia"
    assert jobs[0].workplace_type == "remote"
    assert jobs[0].salary == "Зарплата: 4 000–5 000 EUR"


async def test_we_work_remotely_parser() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss><channel><item>
      <title>Acme: QA Lead</title>
      <region>Anywhere in the World</region>
      <category>Programming</category>
      <type>Full-Time</type>
      <description>&lt;p&gt;Automation role&lt;/p&gt;</description>
      <pubDate>Mon, 07 Sep 2026 14:09:46 +0000</pubDate>
      <guid>https://weworkremotely.com/jobs/1</guid>
      <link>https://weworkremotely.com/jobs/1</link>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await WeWorkRemotelySource().fetch(client)
    assert jobs[0].company == "Acme"
    assert jobs[0].title == "QA Lead"
    assert jobs[0].location == "Anywhere in the World"
