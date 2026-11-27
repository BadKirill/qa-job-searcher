import httpx
from defusedxml import ElementTree as ET

from qa_job_bot.config import Board
from qa_job_bot.search_models import AGGREGATORS, KINDS, SourceSpec
from qa_job_bot.source_http import SourceClient, SourceNetworkError
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

LABELS = dict(
    zip(
        KINDS,
        (
            "Remotive",
            "Himalayas",
            "We Work Remotely",
            "Remote.com",
            "Hirify",
            "Greenhouse",
            "Lever",
            "Ashby",
            "Публичный Telegram",
        ),
        strict=True,
    )
)
LIMITATIONS = {
    "remotive": "Публичная выгрузка агрегатора; не все вакансии рынка.",
    "himalayas": "Первая страница для каждого из первых восьми поисковых запросов.",
    "weworkremotely": "Текущее окно RSS; вакансии вне ленты не доступны.",
    "remote_com": "До 100 результатов на каждый из первых восьми запросов; нужны доступные детали.",
    "hirify": "Первые три страницы API; выдача может быть неполной.",
    "greenhouse": "Одна карьерная доска; недоступные детали пропускаются.",
    "lever": "Одна доска в выбранном регионе API.",
    "ashby": "Только опубликованные вакансии одной доски.",
    "telegram": (
        "Две страницы публичного канала; это не полный архив "
        "и не подтверждение подлинности объявления."
    ),
}


DESCRIPTIONS = {
    "remotive": "Удалённые вакансии из открытой подборки Remotive для разных профессий.",
    "himalayas": "Удалённые вакансии по вашим запросам. Доступны первые результаты каждой фразы.",
    "weworkremotely": "Недавние удалённые вакансии в разработке, дизайне и поддержке.",
    "remote_com": "Объявления компаний, нанимающих удалённо. До 100 вакансий на поисковую фразу.",
    "hirify": "Вакансии технологических компаний. Доступны три первые страницы.",
}


def catalog():
    return [
        {
            "kind": kind,
            "name": LABELS[kind],
            "limitations": LIMITATIONS[kind],
            "builtin": kind in AGGREGATORS,
            "description": DESCRIPTIONS.get(kind),
            "actions": ["enable", "check"]
            if kind in AGGREGATORS
            else ["enable", "check", "edit", "delete"],
        }
        for kind in KINDS
    ]


def build_personal_sources(rows, profile):
    sources = []
    queries = (profile.source_queries or profile.role_keywords)[:8] or ("",)
    for row in rows:
        if not row["enabled"]:
            continue
        spec = SourceSpec(kind=row["kind"], config=row["config"])
        config = spec.normalized()
        kind = spec.kind
        if kind == "remotive":
            source = RemotiveSource()
        elif kind == "himalayas":
            source = HimalayasSource(queries)
        elif kind == "weworkremotely":
            source = WeWorkRemotelySource()
        elif kind == "remote_com":
            source = RemoteComSource(queries)
        elif kind == "hirify":
            source = HirifySource()
        elif kind == "telegram":
            terms = profile.role_keywords + (
                ("qa", "sdet", "тестировщик") if profile.qa_title_fallback else ()
            )
            source = TelegramChannelSource(config["handle"], terms)
        else:
            board = Board(
                kind, config["name"], config["token"], config["region"], tuple(config["tags"])
            )
            source = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource}[
                kind
            ](board)
        sources.append(source)
    return sources


def probe_request(kind, config, queries):
    query = queries[0] if queries else ""
    endpoints = {
        "remotive": (RemotiveSource.endpoint, {}),
        "himalayas": (HimalayasSource.endpoint, {"q": query, "page": 1}),
        "weworkremotely": (WeWorkRemotelySource.endpoint, {}),
        "remote_com": (RemoteComSource.endpoint, {"query": query, "page_size": 1}),
        "hirify": (HirifySource.endpoint, {"page": 1}),
    }
    if kind in AGGREGATORS:
        return endpoints[kind]
    if kind == "telegram":
        return f"https://t.me/s/{config['handle']}", {}
    token = config["token"]
    if kind == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", {}
    if kind == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}", {}
    host = "api.eu.lever.co" if config["region"] == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{token}", {"mode": "json"}


def probe_count(kind, response):
    if kind == "weworkremotely":
        root = ET.fromstring(response.content)
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("invalid_feed")
        return len(root.findall("./channel/item"))
    if kind == "telegram":
        if "tgme_channel_info" not in response.text and "tgme_widget_message" not in response.text:
            raise ValueError("not_public_channel")
        return response.text.count('data-post="')
    data = response.json()
    values = (
        data.get("data", {}).get("jobs")
        if kind == "remote_com"
        else data.get("data")
        if kind == "hirify"
        else data
        if kind == "lever"
        else data.get("jobs")
    )
    if not isinstance(values, list):
        raise ValueError("invalid_response")
    return len(values)


async def probe_source(spec, queries, client_factory=SourceClient):
    try:
        config = spec.normalized()
        url, params = probe_request(spec.kind, config, queries)
        async with client_factory(max_requests=1) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            count = probe_count(spec.kind, response)
        return {"status": "ok" if count else "empty", "items": count, "reason": None}
    except httpx.HTTPStatusError as error:
        reason = (
            "redirect"
            if error.response.is_redirect
            else "rate_limited"
            if error.response.status_code == 429
            else "http_error"
        )
    except (httpx.RequestError, TimeoutError, OSError):
        reason = "unavailable"
    except (ValueError, TypeError, AttributeError, ET.ParseError) as error:
        reason = str(error) if isinstance(error, SourceNetworkError) else "invalid_response"
    return {"status": "unavailable", "items": 0, "reason": reason}
