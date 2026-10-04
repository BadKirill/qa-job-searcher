import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from qa_job_bot import history_import as history
from qa_job_bot.config import load_profile
from qa_job_bot.models import Job
from qa_job_bot.web_store import WebStore

DESCRIPTION = (
    "Remote worldwide. Build automated tests with Playwright, Python and API testing. " * 3
)


@pytest.fixture
def profile():
    return load_profile(Path("config/search_profile.toml"))


def job(**changes):
    return Job(
        **{
            "source": "Hirify",
            "source_id": "123",
            "title": "Senior QA Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "url": "https://hirify.me/jobs/123-qa",
            "description": DESCRIPTION,
            "remote": True,
            "workplace_type": "remote",
            **changes,
        }
    )


def row(value=None, **changes):
    value = value or job()
    return {
        **asdict(value),
        "fingerprint": value.fingerprint,
        "payload": json.dumps(asdict(value), default=str),
        "delivered_at": history.SINCE,
        **changes,
    }


def html_page(**changes):
    data = {
        "id": 123,
        "slug": "123-qa",
        "title": "Senior QA Engineer",
        "company_title": "Acme",
        "work_format": ["remote"],
        "remote_type": "global",
        "salary": {"min": 5000},
        "tags": [{"name": "Python"}],
        "grades": ["senior"],
        "updated_at": "2026-10-01",
        "is_archived": False,
        "is_scam": False,
        **changes,
    }
    values = []

    def encode(value):
        index = len(values)
        values.append(None)
        if isinstance(value, dict):
            values[index] = {key: encode(item) for key, item in value.items()}
        elif isinstance(value, list):
            values[index] = [encode(item) for item in value]
        else:
            values[index] = value
        return index

    encode(data)
    return (
        '<div class="description"><p>' + DESCRIPTION + "</p><br><div>API</div></div>"
        '<script id="__NUXT_DATA__">' + json.dumps(values) + "</script>"
    )


class Reader:
    requests = 0
    paused = {}
    next_retry_at = {}

    def __init__(self, text="", error=None):
        self.text = text
        self.error = error

    async def get(self, url):
        history.public_url(url)
        if self.error:
            raise self.error
        return self.text


@pytest.mark.parametrize(
    "url",
    [
        "http://hirify.me/jobs/123",
        "https://evil.com/jobs/123",
        "https://hirify.me:443/jobs/123",
        "https://name@hirify.me/jobs/123",
        "https://hirify.me/jobs/123#x",
        "https://hirify.me/jobs/../secret",
        "https://hirify.me/jobs/123?x=1",
        "https://hirify.me/jobs/%2fsecret",
        "https://127.0.0.1/jobs/123",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(history.ImportFailure):
        history.public_url(url)


async def test_reader_pins_dns_and_rejects_redirect(monkeypatch):
    monkeypatch.setattr(history, "public_address", AsyncMock(return_value="8.8.8.8"))
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(200, content="Описание вакансии".encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await history.PublicReader(client).get(job().url) == "Описание вакансии"
    assert observed[0].url.host == "8.8.8.8"
    assert observed[0].headers["host"] == "hirify.me"
    assert observed[0].extensions["sni_hostname"] == "hirify.me"
    for status in (302, 403, 404, 429):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, status=status: httpx.Response(
                    status, headers={"retry-after": "120"}
                )
            )
        ) as client:
            reader = history.PublicReader(client)
            with pytest.raises(history.ImportFailure, match=f"HTTP {status}"):
                await reader.get(job().url)
            if status == 429:
                with pytest.raises(history.ImportFailure, match="Retry-After: 120"):
                    await reader.get(job().url)
                assert reader.requests == 1


async def test_reader_body_limit_and_address_validation(monkeypatch):
    resolver = AsyncMock(return_value=[(0, 0, 0, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await history.public_address("hirify.me") == "8.8.8.8"
    for addresses in ([], [(0, 0, 0, "", ("127.0.0.1", 443))]):
        resolver.return_value = addresses
        with pytest.raises(history.ImportFailure, match="non-public"):
            await history.public_address("hirify.me")
    monkeypatch.setattr(history, "public_address", AsyncMock(return_value="8.8.8.8"))
    monkeypatch.setattr(history, "MAX_BYTES", 3)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"1234"))
    ) as client:
        with pytest.raises(history.ImportFailure, match="size limit"):
            await history.PublicReader(client).get(job().url)


def test_public_hirify_metadata_and_description():
    value, status = history.hirify_job(html_page(), row())
    assert status == "public_page_available"
    assert value.location == "Worldwide" and value.tags == ("Python", "senior")
    assert value.published_at.year == 2026 and value.salary.startswith("5,000")
    assert "API" in value.description and value.remote
    value, _ = history.hirify_job(html_page(company_title="%hidden%", work_format=[]), row())
    assert value.company == "Company hidden" and value.workplace_type == ""
    value, _ = history.hirify_job(html_page(work_format=["hybrid"]), row())
    assert value.workplace_type == "hybrid" and not value.remote
    for key in ("is_archived", "is_scam", "is_potential_scam"):
        with pytest.raises(history.VacancyRejected):
            history.hirify_job(html_page(**{key: True}), row())
    with pytest.raises(history.ImportFailure, match="Matching vacancy"):
        history.hirify_job(html_page(id=999), row())
    with pytest.raises(history.ImportFailure, match="description"):
        history.hirify_job(html_page().replace(DESCRIPTION, ""), row())
    for state in ("{}", "[0]" * 1):
        with pytest.raises(history.ImportFailure):
            history.hirify_job('<script id="__NUXT_DATA__">' + state + "</script>", row())


def test_page_parser_and_bounded_metadata():
    page = history.VacancyPage()
    page.feed(
        '<div class="description"><script>secret</script><style>hidden</style>'
        '<h2>Tasks</h2><p>visible<img src="x"><br>second</p></div><p>outside</p>'
    )
    assert page.text == "Tasks\nvisible\nsecond"
    for values, index, depth in (([1], -1, 0), ([1], True, 0), ([1], "0", 0), ([1], 0, 9)):
        with pytest.raises(history.ImportFailure):
            history._decode(values, index, depth)
    with pytest.raises(history.ImportFailure):
        history._decode([[0]], 0)
    assert history.saved_job(row()).description == DESCRIPTION
    assert history.saved_job(row(payload=None)) is None


@pytest.mark.parametrize("tag", sorted(history.VacancyPage.void_tags))
@pytest.mark.parametrize("ending", [">", "/>", " />"])
def test_description_survives_void_elements_without_capturing_surroundings(tag, ending):
    page = history.VacancyPage()
    page.feed(
        '<aside>Unrelated role</aside><div class="description"><strong>Published time</strong>:'
        f" 2026-09-26<{tag}{ending}<section><p>Remote worldwide</p>"
        f"<{tag}{ending}<p>Requirements: Python and API testing</p></section>"
        '</div><div class="original-text-paywall">Locked text</div><p>Other vacancy</p>'
    )
    assert "Published time: 2026-09-26" in page.text
    assert "Remote worldwide" in page.text
    assert "Requirements: Python and API testing" in page.text
    assert "Unrelated" not in page.text and "Locked" not in page.text
    assert "Other vacancy" not in page.text


def test_self_closing_nested_elements_and_void_end_tags_keep_description_boundaries():
    page = history.VacancyPage()
    page.feed(
        '<div class="description"><div/>First<br/></br><span/>Second'
        "<script/>Third<style/>Fourth<img/></img>Fifth</div>"
        '<p>Outside</p><script id="__NUXT_DATA__">["hidden"]</script>'
    )
    assert page.text == "First\nSecondThirdFourthFifth"
    assert "".join(page.state_parts) == '["hidden"]'


@pytest.mark.parametrize("length,accepted", [(79, False), (80, True), (81, True)])
def test_full_description_length_after_self_closing_break(length, accepted):
    prefix = "Published time: 2026-09-26\n"
    description = prefix + "x" * (length - len(prefix))
    body = '<div class="description">' + description.replace("\n", "<br />") + "</div>"
    state_tag = '<script id="__NUXT_DATA__">'
    html = body + state_tag + html_page().split(state_tag, 1)[1]
    if accepted:
        value, _ = history.hirify_job(html, row())
        assert value.description == description and len(value.description) == length
    else:
        with pytest.raises(history.ImportFailure, match="Full public description"):
            history.hirify_job(html, row())


def test_hidden_metadata_and_paywall_do_not_replace_missing_public_description():
    html = html_page(description=DESCRIPTION).replace(
        'class="description"', 'class="original-text-paywall"'
    )
    with pytest.raises(history.ImportFailure, match="Full public description"):
        history.hirify_job(html, row())


async def test_restore_source_integrations():
    value, method, status = await history.restore_job(row(), Reader(html_page()))
    assert value.source == "Hirify" and method == "hirify_public_page"
    value, method, status = await history.restore_job(
        row(job(source="Telegram · @public")), Reader()
    )
    assert value and method == "stored_telegram_payload" and status == "not_checked"
    remote = row(job(source="Remote.com", source_id="acme/qa"))
    detail = {
        "data": {
            "description": DESCRIPTION,
            "hiring_location": {"type": "global"},
            "workplace_location": {"type": "remote"},
        }
    }
    value, method, status = await history.restore_job(remote, Reader(json.dumps(detail)))
    assert value.location == "Worldwide" and method == "remote_public_api"
    with pytest.raises(history.ImportFailure, match="metadata"):
        await history.restore_job({**remote, "payload": None}, Reader(json.dumps(detail)))
    detail["data"] = {"description": "short"}
    with pytest.raises(history.ImportFailure, match="description"):
        await history.restore_job(remote, Reader(json.dumps(detail)))
    himalayas = row(job(source="Himalayas", source_id="https://himalayas.app/companies/a/jobs/b"))
    item = {
        "guid": himalayas["source_id"],
        "title": "QA Engineer",
        "description": DESCRIPTION,
        "companyName": "Acme",
        "locationRestrictions": ["Europe"],
    }
    value, method, status = await history.restore_job(
        himalayas, Reader(json.dumps({"jobs": [item]}))
    )
    assert value.location == "Europe" and method == "himalayas_public_api"
    for jobs in ([], [{**item, "description": ""}]):
        with pytest.raises(history.ImportFailure, match="Himalayas"):
            await history.restore_job(himalayas, Reader(json.dumps({"jobs": jobs})))
    with pytest.raises(history.ImportFailure, match="Unsupported"):
        await history.restore_job(row(job(source="Unknown")), Reader())


async def test_report_revalidates_deduplicates_and_records_failures(profile, monkeypatch):
    rows = [
        row(),
        row(job(company="Duplicate")),
        row(job(source_id="2", company="Missing"), payload=None),
    ]
    report = await history.build_report(
        rows, profile, Reader(error=history.ImportFailure("HTTP 429"))
    )
    assert report["counts"]["accepted"] == 1
    assert report["counts"]["duplicate"] == 1 and report["counts"]["unavailable"] == 1
    assert report["entries"][0]["availability"] == "unknown"
    rejected = await history.build_report([row()], profile, Reader(html_page(is_archived=True)))
    assert rejected["counts"]["closed_or_flagged"] == 1
    assert rejected["entries"][0]["status"] == "rejected"
    filtered = await history.build_report([row()], profile, Reader(html_page(title="Junior QA")))
    assert filtered["entries"][0]["status"] == "rejected"
    monkeypatch.setattr(history, "rank_jobs", lambda jobs, profile: jobs)
    monkeypatch.setattr(
        history,
        "match_job",
        lambda job, profile: type(
            "Result", (), {"location_status": "unknown", "reasons": ["unknown"]}
        )(),
    )
    filtered = await history.build_report([row()], profile, Reader(html_page()))
    assert filtered["entries"][0]["location_status"] == "unknown"


async def test_resume_keeps_successes_and_retries_only_failures(profile):
    report = await history.build_report([row()], profile, Reader(html_page()))
    resumed = await history.build_report(
        [row(already_current=True, target_id="canonical")],
        profile,
        Reader(error=AssertionError("no request")),
        report,
    )
    assert resumed["entries"][0]["reused_from"] == report["checked_at"]
    assert resumed["entries"][0]["target_id"] == "canonical"
    assert resumed["counts"]["already_current"] == 1
    report["profile_hash"] = "changed"
    refreshed = await history.build_report(
        [row()], profile, Reader(html_page(title="Junior QA")), report
    )
    assert refreshed["counts"]["rejected"] == 1


@pytest.fixture
def store(tmp_path):
    value = WebStore(tmp_path / "jobs.sqlite3")
    value.initialize()
    value.upsert_jobs([job()])
    value.mark_delivered(1, [job()])
    return value


async def test_apply_preserves_states_deliveries_and_current_ids(store, profile):
    store.change_state(job().fingerprint, {"applied": True, "archived": True})
    store.set_setting("current_jobs", [job().fingerprint])
    rows = history.read_history(store.path, history.SINCE)
    report = await history.build_report(rows, profile, Reader(html_page(company_title="Renamed")))
    before = store.get_job(job().fingerprint)
    accepted = history.apply_report(store.path, report)
    assert accepted == [job().fingerprint]
    assert store.get_setting("current_jobs") == [job().fingerprint]
    after = store.get_job(job().fingerprint)
    assert after["company"] == "Renamed"
    assert after["archived_at"] == before["archived_at"]
    assert after["applied_at"] == before["applied_at"]
    assert len(history.read_history(store.path, history.SINCE)) == 1
    assert history.apply_report(store.path, report) == accepted
    report["entries"][0]["source_id"] = "changed"
    with pytest.raises(history.ImportFailure, match="identity changed"):
        history.apply_report(store.path, report)
    assert store.get_setting("imported_jobs") == accepted
    with pytest.raises(history.ImportFailure, match="timestamp"):
        history.read_history(store.path, "invalid")


async def test_deduplication_uses_current_canonical_identity(store, profile):
    historical = job()
    canonical = job(company="Current company")
    store.upsert_jobs([canonical])
    store.set_setting("current_jobs", [canonical.fingerprint])
    rows = history.read_history(store.path, history.SINCE)
    assert rows[0]["target_id"] == canonical.fingerprint
    report = await history.build_report(rows, profile, Reader(html_page()))
    assert history.apply_report(store.path, report) == [canonical.fingerprint]
    assert store.list_jobs("active", 1)["total"] == 1
    with closing(sqlite3.connect(store.path)) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE fingerprint=?", (historical.fingerprint,)
            ).fetchone()[0]
            == 1
        )


async def test_run_and_main_are_reviewable_by_default(
    store, profile, tmp_path, monkeypatch, capsys
):
    reader = Reader(html_page())
    monkeypatch.setattr(history, "PublicReader", lambda client: reader)
    args = argparse.Namespace(
        database=store.path,
        since=history.SINCE,
        profile=Path("config/search_profile.toml"),
        apply=False,
        report=tmp_path / "nested/report.json",
        resume=None,
    )
    report = await history.run(args)
    assert not report["applied"] and report["total"] == 1
    assert store.get_setting("imported_jobs") is None
    assert args.report.stat().st_mode & 0o777 == 0o600
    args.apply, args.resume = True, args.report
    report = await history.run(args)
    assert report["imported_ids"] == [job().fingerprint]
    monkeypatch.setattr(history.argparse.ArgumentParser, "parse_args", lambda self: args)
    monkeypatch.setattr(history, "run", lambda args: report)
    monkeypatch.setattr(history.asyncio, "run", lambda value: value)
    history.main()
    assert json.loads(capsys.readouterr().out)["applied"] is True


async def test_resume_obeys_retry_after_before_network(tmp_path):
    saved = tmp_path / "report.json"
    saved.write_text(
        json.dumps(
            {"next_retry_at": {"hirify.me": (history.utcnow() + timedelta(minutes=30)).isoformat()}}
        )
    )
    store = WebStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    args = argparse.Namespace(database=store.path, since=history.SINCE, resume=saved)
    with pytest.raises(history.ImportFailure, match="not permitted before"):
        await history.run(args)


@pytest.mark.parametrize("delay", ["Wed, 07 Oct 2026 10:00:00 GMT", "invalid"])
async def test_retry_after_date_and_missing_header(delay, monkeypatch):
    monkeypatch.setattr(history, "public_address", AsyncMock(return_value="8.8.8.8"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"retry-after": delay})
        )
    ) as client:
        reader = history.PublicReader(client)
        with pytest.raises(history.ImportFailure):
            await reader.get(job().url)
        assert history.parse_datetime(reader.next_retry_at["hirify.me"]) is not None
