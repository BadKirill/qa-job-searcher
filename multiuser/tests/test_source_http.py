import socket

import httpx
import pytest

from qa_job_bot.search_models import KINDS, SourceSpec
from qa_job_bot.source_catalog import probe_count, probe_request, probe_source
from qa_job_bot.source_http import (
    PinnedTransport,
    SourceClient,
    SourceNetworkError,
    public_addresses,
    public_ip,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "fc00::1",
        "192.0.2.1",
        "224.0.0.1",
    ],
)
def test_nonpublic_addresses_are_rejected(address):
    assert not public_ip(address)


async def test_resolver_deduplicates_and_uses_https_port(monkeypatch):
    calls = []

    def resolve(host, port, **kwargs):
        calls.append((host, port, kwargs))
        return [(0, 0, 0, "", ("8.8.8.8", 443))] * 2

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert await public_addresses("remotive.com") == ["8.8.8.8"]
    assert calls == [("remotive.com", 443, {"type": socket.SOCK_STREAM})]


async def test_dns_is_pinned_per_hostname_with_original_host_tls_sni_and_separate_pool():
    resolutions, requests, transports = [], [], []

    async def resolve(host):
        resolutions.append(host)
        return ["8.8.8.8"] if resolutions.count(host) == 1 else ["127.0.0.1"]

    def factory():
        transport = httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"jobs": []})
        )
        transports.append(transport)
        return transport

    pinned = PinnedTransport(resolve, factory)
    async with SourceClient(transport=pinned) as client:
        await client.get("https://remotive.com/api/remote-jobs")
        await client.get("https://remotive.com/api/remote-jobs?q=second")
        await client.get("https://himalayas.app/jobs/api/search")
    assert resolutions == ["remotive.com", "himalayas.app"] and len(transports) == 2
    assert all(request.url.host == "8.8.8.8" for request in requests)
    assert [request.headers["host"] for request in requests] == [
        "remotive.com",
        "remotive.com",
        "himalayas.app",
    ]
    assert [request.extensions["sni_hostname"] for request in requests] == [
        "remotive.com",
        "remotive.com",
        "himalayas.app",
    ]


@pytest.mark.parametrize(
    "url,method",
    [
        ("http://remotive.com", "GET"),
        ("https://evil.test", "GET"),
        ("https://127.0.0.1", "GET"),
        ("https://remotive.com:8080", "GET"),
        ("https://user:password@remotive.com", "GET"),
        ("https://remotive.com", "POST"),
    ],
)
async def test_transport_rejects_invalid_target_before_resolution(url, method):
    async def forbidden(host):
        pytest.fail("Unexpected DNS lookup")

    async with SourceClient(transport=PinnedTransport(forbidden)) as client:
        with pytest.raises(SourceNetworkError, match="blocked_address"):
            await client.request(method, url)


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["8.8.8.8", "10.0.0.1"]])
async def test_transport_checks_all_dns_answers(addresses):
    async def resolve(host):
        return addresses

    async with SourceClient(transport=PinnedTransport(resolve)) as client:
        with pytest.raises(SourceNetworkError, match="blocked_resolution"):
            await client.get("https://remotive.com")


async def test_request_limits_and_redirect_are_not_followed():
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    async with SourceClient(transport=httpx.MockTransport(response), max_requests=1) as client:
        result = await client.get("https://remotive.com")
        assert result.status_code == 302 and len(requests) == 1
        with pytest.raises(SourceNetworkError, match="request_limit"):
            await client.get("https://remotive.com")


class Chunks(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"123"
        yield b"456"

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "headers,max_response,max_total,reason",
    [
        ({"content-encoding": "gzip"}, 10, 10, "unsupported_encoding"),
        ({"content-length": "bad"}, 10, 10, "response_limit"),
        ({"content-length": "20"}, 10, 10, "response_limit"),
        ({}, 5, 10, "response_limit"),
        ({}, 10, 5, "response_limit"),
    ],
)
async def test_bounded_streams_close_on_failure(headers, max_response, max_total, reason):
    stream = Chunks()
    async with SourceClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=stream)
        ),
        max_response=max_response,
        max_total=max_total,
    ) as client:
        with pytest.raises(SourceNetworkError, match=reason):
            await client.get("https://remotive.com")
    assert stream.closed


async def test_run_total_bytes_across_responses():
    async with SourceClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"1234")),
        max_total=7,
    ) as client:
        assert (await client.get("https://remotive.com")).text == "1234"
        with pytest.raises(SourceNetworkError):
            await client.get("https://remotive.com")


@pytest.mark.parametrize("kind", KINDS)
async def test_each_supported_source_has_a_single_request_probe(kind):
    config = (
        {}
        if kind in KINDS[:5]
        else {"handle": "test_channel"}
        if kind == "telegram"
        else {"token": "acme", "region": "eu" if kind == "lever" else "global"}
    )
    requests = []

    def response(request):
        requests.append(request)
        if kind == "weworkremotely":
            return httpx.Response(200, text="<rss><channel><item/></channel></rss>")
        if kind == "telegram":
            return httpx.Response(
                200, text='<div class="tgme_channel_info" data-post="test_channel/1">'
            )
        payload = (
            {"data": {"jobs": [{}]}}
            if kind == "remote_com"
            else {"data": [{}]}
            if kind == "hirify"
            else [{}]
            if kind == "lever"
            else {"jobs": [{}]}
        )
        return httpx.Response(200, json=payload)

    def factory(**kwargs):
        return SourceClient(transport=httpx.MockTransport(response), **kwargs)

    result = await probe_source(SourceSpec(kind=kind, config=config), ["product designer"], factory)
    assert result == {"status": "ok", "items": 1, "reason": None}
    assert len(requests) == 1
    if kind in {"himalayas", "remote_com"}:
        assert "product designer" in str(requests[0].url).replace("+", " ").replace("%20", " ")
    if kind == "lever":
        assert requests[0].url.host == "api.eu.lever.co"


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("empty", None),
        ("redirect", "redirect"),
        ("429", "rate_limited"),
        ("500", "http_error"),
        ("json", "invalid_response"),
        ("schema", "invalid_response"),
        ("timeout", "unavailable"),
        ("dns", "unavailable"),
        ("private", "blocked_resolution"),
    ],
)
async def test_probe_distinguishes_empty_unavailable_and_invalid_responses(mode, reason):
    def response(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("secret internal host", request=request)
        if mode == "dns":
            raise OSError("secret DNS data")
        if mode == "private":
            raise SourceNetworkError("blocked_resolution")
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1"})
        if mode.isdecimal():
            return httpx.Response(int(mode))
        return (
            httpx.Response(200, text="invalid")
            if mode == "json"
            else httpx.Response(200, json={} if mode == "schema" else {"jobs": []})
        )

    def factory(**kwargs):
        return SourceClient(transport=httpx.MockTransport(response), **kwargs)

    result = await probe_source(SourceSpec(kind="remotive"), [], factory)
    assert result["status"] == ("empty" if mode == "empty" else "unavailable")
    assert result["reason"] == reason and "secret" not in str(result)


@pytest.mark.parametrize(
    "kind,text",
    [
        ("weworkremotely", "<html/>"),
        ("telegram", "<html>private</html>"),
        ("weworkremotely", "<broken"),
    ],
)
def test_probe_rejects_wrong_feed_shapes(kind, text):
    with pytest.raises(ValueError if kind == "telegram" or text == "<html/>" else Exception):
        probe_count(kind, httpx.Response(200, text=text))


def test_global_lever_probe_uses_global_api():
    assert probe_request("lever", {"token": "company", "region": "global"}, [])[0].startswith(
        "https://api.lever.co/"
    )


async def test_board_response_budget_is_bounded_and_explicit_lower_limits_are_preserved():
    from qa_job_bot.source_http import MAX_BOARD_RESPONSE, MAX_RESPONSE, RunBudget

    default = SourceClient()
    assert default.response_limit == MAX_RESPONSE
    default.bind_budget(RunBudget(), 16, MAX_BOARD_RESPONSE)
    assert default.response_limit == MAX_BOARD_RESPONSE
    explicit = SourceClient(max_response=100, max_requests=1)
    explicit.bind_budget(RunBudget(), 16, MAX_BOARD_RESPONSE)
    assert explicit.response_limit == 100 and explicit.request_limit == 1

    await default.aclose()
    await explicit.aclose()


def test_exhausted_byte_budget_blocks_new_requests_before_the_network():
    from qa_job_bot.source_http import RunBudget

    budget = RunBudget(bytes_used=7, total_limit=7)
    with pytest.raises(SourceNetworkError, match="run_response_limit"):
        budget.request()
    assert budget.requests_used == 0
