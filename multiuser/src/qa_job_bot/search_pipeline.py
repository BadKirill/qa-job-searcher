import asyncio
import copy
from dataclasses import dataclass, replace
from datetime import datetime

from qa_job_bot.matching import match_job
from qa_job_bot.models import Job, utcnow
from qa_job_bot.search_models import SearchSpec
from qa_job_bot.source_catalog import build_personal_sources
from qa_job_bot.source_http import (
    MAX_BOARD_RESPONSE,
    MAX_RESPONSE,
    MAX_RUN_SOURCES,
    RunBudget,
    SourceClient,
    SourceNetworkError,
    source_error_code,
)
from qa_job_bot.sources.base import PartialSourceError
from qa_job_bot.web_store import digest

ADAPTER_VERSION = 2


def source_key(source, queries=None):
    config = {key: value for key, value in source["config"].items() if key not in {"name", "tags"}}
    return digest(
        {
            "adapter": source["kind"],
            "version": ADAPTER_VERSION,
            "config": config,
            **({"queries": queries} if queries is not None else {}),
        }
    )


def source_queries(profile):
    return list((profile.source_queries or profile.role_keywords)[:8])


def load_job(data):
    values = {key: value for key, value in data.items() if key in Job.__dataclass_fields__}
    values["published_at"] = (
        datetime.fromisoformat(values["published_at"]) if values.get("published_at") else None
    )
    return Job(**values)


def normalize_public_job(job):
    data = replace(
        job, score=0, match_reasons=[], location_status="unknown", resolved_fingerprint=None
    )
    values = [
        data.source,
        data.source_id,
        data.title,
        data.company,
        data.location,
        data.url,
        data.description,
        data.workplace_type,
        data.employment_type,
        data.salary,
        *data.tags,
        *data.location_restrictions,
        *data.excluded_locations,
    ]
    for value in values:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("invalid_source_text")
        value.encode("utf-8")
    return data


@dataclass
class SourceBatch:
    source: dict
    jobs: list[Job]
    fetched_at: str
    error: str | None = None


class PersonalSearchPipeline:
    def __init__(self, client_factory=None, timeout=25):
        self.client_factory = client_factory
        self.timeout = timeout

    async def fetch(self, inputs):
        profile = SearchSpec.model_validate(inputs["profile"]).runtime()
        if len(inputs["sources"]) > MAX_RUN_SOURCES:
            raise SourceNetworkError("source_limit")
        factory = self.client_factory
        if factory is None:

            def factory():
                return SourceClient(timeout=self.timeout)

        budget, slots = RunBudget(), asyncio.Semaphore(4)

        async def collect(source):
            async with slots, factory() as client:
                bind = getattr(client, "bind_budget", None)
                if bind is not None:
                    bind(
                        budget,
                        1024 if source["kind"] == "remote_com" else 16,
                        MAX_BOARD_RESPONSE if source["kind"] == "greenhouse" else MAX_RESPONSE,
                    )
                public = copy.deepcopy(source)
                public["config"].pop("tags", None)
                if "token" in public["config"]:
                    public["config"]["name"] = public["config"]["token"]
                public_profile = (
                    replace(profile, role_keywords=(), qa_title_fallback=False)
                    if source["kind"] == "telegram"
                    else profile
                )
                adapter = build_personal_sources([public], public_profile)[0]
                try:
                    failure = None
                    try:
                        raw = await adapter.fetch(client)
                    except PartialSourceError as error:
                        raw, failure = error.jobs, source_error_code(error.cause)
                    jobs = [normalize_public_job(job) for job in raw]
                    return SourceBatch(source, jobs, utcnow().isoformat(), failure)
                except Exception as error:
                    return SourceBatch(source, [], utcnow().isoformat(), source_error_code(error))

        return await asyncio.gather(*(collect(source) for source in inputs["sources"]))


def personal_match(job, source, profile):
    local = replace(job, tags=(*job.tags, *source["config"].get("tags", [])))
    return match_job(local, profile)
