import asyncio

import httpx
import pytest

from qa_job_bot.config import Board
from qa_job_bot.search_models import SearchSpec
from qa_job_bot.search_pipeline import PersonalSearchPipeline
from qa_job_bot.source_http import RunBudget, SourceClient, SourceNetworkError, source_error_code
from qa_job_bot.sources import GreenhouseSource, HimalayasSource, HirifySource, RemoteComSource
from qa_job_bot.sources.base import PartialSourceError


def inputs(*sources):
    return {
        "profile": SearchSpec(role_keywords=["backend engineer"]).model_dump(),
        "sources": [{"kind": kind, "enabled": True, "config": config} for kind, config in sources],
    }


def telegram_page():
    return (
        '<div class="tgme_widget_message_wrap"><div data-post="public_jobs/42">'
        '<div class="tgme_widget_message_text">Backend Engineer<br>Remote Worldwide</div>'
        '</div></div><div class="tgme_channel_info"></div>'
    )


async def test_large_boards_do_not_exhaust_other_sources_and_clients_are_closed():
    clients, requests = [], []

    def response(request):
        requests.append(request)
        if request.url.host == "t.me":
            return httpx.Response(200, text=telegram_page())
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": i, "title": "Backend Engineer", "content": "Remote Worldwide"}
                        for i in range(300)
                    ]
                },
            )
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "jobs": [
                            {"slug": f"job-{i}", "company_profile": {"slug": "acme"}}
                            for i in range(260)
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"data": {"description": "Full description"}})

    def factory():
        client = SourceClient(transport=httpx.MockTransport(response))
        clients.append(client)
        return client

    batches = await PersonalSearchPipeline(factory).fetch(
        inputs(
            ("greenhouse", {"token": "acme"}),
            ("remote_com", {}),
            ("telegram", {"handle": "public_jobs"}),
        )
    )
    assert [len(batch.jobs) for batch in batches] == [300, 260, 1]
    assert all(batch.error is None for batch in batches)
    assert len(requests) == 264 and all(client.is_closed for client in clients)
    assert len({id(client.run_budget) for client in clients}) == 1


@pytest.mark.parametrize("failure", ["429", "500", "timeout", "json"])
@pytest.mark.parametrize("kind", ["himalayas", "hirify", "remote_com"])
async def test_partial_query_page_and_detail_failures_preserve_valid_jobs(kind, failure):
    def response(request):
        if kind == "remote_com" and request.url.path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "jobs": [
                            {"slug": slug, "company_profile": {"slug": "acme"}}
                            for slug in ["good", "bad"]
                        ]
                    }
                },
            )
        good = (
            request.url.params.get("q") == "good"
            if kind == "himalayas"
            else request.url.params.get("page") == "1"
            if kind == "hirify"
            else request.url.path.endswith("/good")
        )
        if good:
            payload = (
                {"jobs": [{"guid": "good"}]}
                if kind == "himalayas"
                else {"data": [{"id": "good"}]}
                if kind == "hirify"
                else {"data": {"description": "Full body"}}
            )
            return httpx.Response(200, json=payload)
        if failure == "timeout":
            raise httpx.ReadTimeout("private provider response", request=request)
        if failure == "json":
            return httpx.Response(200, text="invalid JSON")
        return httpx.Response(int(failure))

    source = (
        HimalayasSource(("good", "bad"))
        if kind == "himalayas"
        else HirifySource()
        if kind == "hirify"
        else RemoteComSource()
    )
    async with SourceClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(PartialSourceError) as caught:
            await source.fetch(client)
    assert len(caught.value.jobs) == 1
    assert (
        source_error_code(caught.value.cause)
        == {
            "429": "HTTP_429",
            "500": "HTTP_500",
            "timeout": "ReadTimeout",
            "json": "JSONDecodeError",
        }[failure]
    )
    assert "private" not in str(caught.value)


async def test_telegram_second_page_failure_keeps_first_and_obeys_explicit_request_limit():
    pipeline = PersonalSearchPipeline(
        lambda: SourceClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=telegram_page())
            ),
            max_requests=1,
        )
    )
    batches = await pipeline.fetch(inputs(("telegram", {"handle": "public_jobs"})))
    assert len(batches[0].jobs) == 1 and batches[0].error == "request_limit"


async def test_partial_invalid_text_cannot_escape_normalization_and_abort_other_sources():
    def response(request):
        if request.url.host == "t.me":
            if request.url.params.get("before"):
                return httpx.Response(500)
            return httpx.Response(200, text=telegram_page().replace("Backend", "Bad\0Backend"))
        return httpx.Response(200, json={"jobs": []})

    batches = await PersonalSearchPipeline(
        lambda: SourceClient(transport=httpx.MockTransport(response))
    ).fetch(inputs(("telegram", {"handle": "public_jobs"}), ("remotive", {})))
    assert batches[0].error == "ValueError" and batches[0].jobs == []
    assert batches[1].error is None


async def test_source_count_is_bounded_before_any_network_request():
    def forbidden():
        pytest.fail("Client must not be created")

    with pytest.raises(SourceNetworkError, match="source_limit"):
        await PersonalSearchPipeline(forbidden).fetch(inputs(*[("remotive", {})] * 129))


async def test_shared_run_request_and_byte_limits_close_streams():
    budget = RunBudget(request_limit=2, total_limit=7)
    clients = [
        SourceClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"1234"))
        )
        for _ in range(2)
    ]
    for client in clients:
        client.bind_budget(budget, 16)
    try:
        assert (await clients[0].get("https://remotive.com")).text == "1234"
        with pytest.raises(SourceNetworkError, match="run_response_limit"):
            await clients[1].get("https://remotive.com")
        with pytest.raises(SourceNetworkError, match="run_request_limit"):
            await clients[0].get("https://remotive.com")
        assert budget.requests_used == 2 and budget.bytes_used == 8
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


async def test_per_source_request_concurrency_is_bounded():
    active, peak = 0, 0

    async def response(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json={})

    async with SourceClient(transport=httpx.MockTransport(response)) as client:
        await asyncio.gather(*(client.get("https://remotive.com") for _ in range(20)))
    assert peak == 4


async def test_missing_greenhouse_description_is_not_published_as_complete():
    async with SourceClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"jobs": [{"id": 1, "content": "Full body"}, {"id": 2}]}
            )
        )
    ) as client:
        with pytest.raises(PartialSourceError) as caught:
            await GreenhouseSource(Board("greenhouse", "Acme", "acme")).fetch(client)
    assert [job.source_id for job in caught.value.jobs] == ["1"]
    assert isinstance(caught.value.cause, ValueError)


@pytest.mark.parametrize(
    "reason",
    [
        "blocked_address",
        "blocked_resolution",
        "unsupported_encoding",
        "response_limit",
        "source_limit",
        "private details",
    ],
)
def test_only_safe_network_reason_codes_are_exposed(reason):
    assert source_error_code(SourceNetworkError(reason)) == (
        "SourceNetworkError" if reason == "private details" else reason
    )


async def test_full_greenhouse_board_can_exceed_small_feed_budget_without_raising_global_budget():
    from qa_job_bot.source_http import MAX_RESPONSE, MAX_TOTAL

    payload = {"jobs": [{"id": 1, "content": "Full description " + "a" * MAX_RESPONSE}]}
    clients = []

    def factory():
        client = SourceClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        )
        clients.append(client)
        return client

    batches = await PersonalSearchPipeline(factory).fetch(inputs(("greenhouse", {"token": "acme"})))
    assert batches[0].error is None and len(batches[0].jobs) == 1
    assert clients[0].bytes_used > MAX_RESPONSE
    assert clients[0].run_budget.total_limit == MAX_TOTAL
