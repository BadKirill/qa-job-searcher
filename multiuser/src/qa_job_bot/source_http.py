import asyncio
import ipaddress
import socket
from dataclasses import dataclass

import httpx

HOSTS = frozenset(
    {
        "remotive.com",
        "himalayas.app",
        "weworkremotely.com",
        "talent-api.remote.com",
        "api.hirify.me",
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.eu.lever.co",
        "api.ashbyhq.com",
        "t.me",
    }
)
MAX_RESPONSE = 8 * 1024 * 1024
MAX_BOARD_RESPONSE = 32 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_REQUESTS = 128
MAX_RUN_REQUESTS = 2048
MAX_RUN_SOURCES = 128


class SourceNetworkError(ValueError):
    pass


@dataclass
class RunBudget:
    requests_used: int = 0
    bytes_used: int = 0
    request_limit: int = MAX_RUN_REQUESTS
    total_limit: int = MAX_TOTAL

    def request(self):
        if self.requests_used >= self.request_limit:
            raise SourceNetworkError("run_request_limit")
        if self.bytes_used >= self.total_limit:
            raise SourceNetworkError("run_response_limit")
        self.requests_used += 1

    def consume(self, size):
        self.bytes_used += size
        if self.bytes_used > self.total_limit:
            raise SourceNetworkError("run_response_limit")


def source_error_code(error):
    if isinstance(error, SourceNetworkError):
        reason = str(error)
        if reason in {
            "request_limit",
            "response_limit",
            "run_request_limit",
            "run_response_limit",
            "blocked_address",
            "blocked_resolution",
            "unsupported_encoding",
            "source_limit",
        }:
            return reason
        return "SourceNetworkError"
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP_{error.response.status_code}"
    return type(error).__name__


async def public_addresses(host):
    result = await asyncio.wait_for(
        asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM), timeout=3
    )
    return list(dict.fromkeys(item[4][0] for item in result))


def public_ip(value):
    address = ipaddress.ip_address(value)
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not (address.version == 6 and address.ipv4_mapped and not address.ipv4_mapped.is_global)
    )


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, resolver=public_addresses, factory=None):
        self.resolver = resolver
        self.factory = factory or (
            lambda: httpx.AsyncHTTPTransport(
                trust_env=False,
                retries=0,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        )
        self.pinned = {}
        self.transports = {}
        self.lock = asyncio.Lock()

    async def handle_async_request(self, request):
        url = request.url
        if (
            request.method != "GET"
            or url.scheme != "https"
            or url.host not in HOSTS
            or url.port not in {None, 443}
            or url.username
            or url.password
        ):
            raise SourceNetworkError("blocked_address")
        async with self.lock:
            if url.host not in self.pinned:
                addresses = await self.resolver(url.host)
                if not addresses or not all(public_ip(value) for value in addresses):
                    raise SourceNetworkError("blocked_resolution")
                self.pinned[url.host] = addresses[0]
                self.transports[url.host] = self.factory()
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=self.pinned[url.host]),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return await self.transports[url.host].handle_async_request(pinned)

    async def aclose(self):
        for transport in self.transports.values():
            await transport.aclose()


class SourceClient(httpx.AsyncClient):
    def __init__(
        self,
        *,
        transport=None,
        timeout=15,
        max_requests=None,
        max_response=None,
        max_total=MAX_TOTAL,
    ):
        super().__init__(
            transport=transport or PinnedTransport(),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "JobSearcher/0.2", "Accept-Encoding": "identity"},
        )
        self.requests_used, self.bytes_used = 0, 0
        self.configured_request_limit = max_requests
        self.configured_response_limit = max_response
        self.request_limit, self.response_limit, self.total_limit = (
            MAX_REQUESTS if max_requests is None else max_requests,
            MAX_RESPONSE if max_response is None else max_response,
            max_total,
        )
        self.run_budget = None
        self.request_slots = asyncio.Semaphore(4)

    def bind_budget(self, budget, request_limit, response_limit=MAX_RESPONSE):
        self.run_budget = budget
        self.request_limit = (
            request_limit
            if self.configured_request_limit is None
            else min(self.configured_request_limit, request_limit)
        )
        self.response_limit = (
            response_limit
            if self.configured_response_limit is None
            else min(self.configured_response_limit, response_limit)
        )

    async def send(self, request, **kwargs):
        async with self.request_slots:
            return await self.bounded_send(request, **kwargs)

    async def bounded_send(self, request, **kwargs):
        if self.requests_used >= self.request_limit:
            raise SourceNetworkError("request_limit")
        if self.run_budget is not None:
            self.run_budget.request()
        self.requests_used += 1
        kwargs["stream"] = True
        response = await super().send(request, **kwargs)
        try:
            encoding = response.headers.get("content-encoding", "identity").casefold()
            if encoding != "identity":
                raise SourceNetworkError("unsupported_encoding")
            length = response.headers.get("content-length")
            if length and (not length.isdecimal() or int(length) > self.response_limit):
                raise SourceNetworkError("response_limit")
            parts, size = [], 0
            async for chunk in response.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                self.bytes_used += len(chunk)
                if self.run_budget is not None:
                    self.run_budget.consume(len(chunk))
                if size > self.response_limit or self.bytes_used > self.total_limit:
                    raise SourceNetworkError("response_limit")
                parts.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(parts),
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()
