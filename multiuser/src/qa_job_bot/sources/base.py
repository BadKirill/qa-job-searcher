from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

import httpx

from qa_job_bot.models import Job


class PartialSourceError(Exception):
    def __init__(self, jobs, cause):
        self.jobs, self.cause = jobs, cause
        super().__init__(type(cause).__name__)


async def collect_responses(requests):
    responses, failures = [], []
    for result in await asyncio.gather(*requests, return_exceptions=True):
        if isinstance(result, Exception):
            failures.append(result)
            continue
        try:
            result.raise_for_status()
            responses.append(result)
        except httpx.HTTPStatusError as error:
            failures.append(error)
    return responses, failures


def finish_fetch(jobs, failures):
    if failures:
        if jobs:
            raise PartialSourceError(jobs, failures[0])
        raise failures[0]
    return jobs


class JobSource(ABC):
    name: str

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        raise NotImplementedError
