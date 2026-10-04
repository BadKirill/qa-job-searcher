from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from qa_job_bot.models import Job


class JobSource(ABC):
    name: str

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        raise NotImplementedError
