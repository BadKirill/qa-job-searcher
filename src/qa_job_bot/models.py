from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_text(value: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", " ", value.casefold()).strip()


@dataclass(slots=True)
class Job:
    source: str
    source_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    published_at: datetime | None = None
    remote: bool | None = None
    workplace_type: str = ""
    employment_type: str = ""
    salary: str = ""
    tags: tuple[str, ...] = ()
    score: int = 0
    match_reasons: list[str] = field(default_factory=list)
    location_status: str = "unknown"
    resolved_fingerprint: str | None = field(default=None, repr=False)

    @property
    def fingerprint(self) -> str:
        return self.resolved_fingerprint or self.legacy_fingerprint

    @property
    def legacy_fingerprint(self) -> str:
        identity = "|".join(
            (
                canonical_text(self.company),
                canonical_text(self.title),
                canonical_text(self.location),
            )
        )
        return sha256(identity.encode()).hexdigest()


@dataclass(slots=True)
class FetchReport:
    jobs: list[Job]
    errors: list[str]
    review_jobs: list[Job] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class MatchResult:
    accepted: bool
    score: int
    reasons: list[str]
    location_status: str
