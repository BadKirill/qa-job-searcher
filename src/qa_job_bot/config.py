from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROLE_KEYWORDS = (
    "head of qa",
    "qa lead",
    "qa manager",
    "quality engineering lead",
    "lead quality engineer",
    "senior qa automation engineer",
    "sdet lead",
    "qa automation architect",
    "test automation architect",
    "ai test engineer",
    "genai test engineer",
    "ai quality engineer",
    "gen(ai) test consultant",
    "quality engineering manager",
    "quality assurance engineer",
    "qa engineer",
    "test automation engineer",
    "software development engineer in test",
    "sdet",
)

DEFAULT_FOCUS_QA_KEYWORDS = (
    "manual qa",
    "manual quality assurance",
    "manual tester",
    "manual testing",
    "ручное тестирование",
    "fullstack qa",
    "full-stack qa",
    "full stack qa",
    "fullstack quality assurance",
    "full-stack quality assurance",
    "full stack quality assurance",
)

EUROPE_CIS_LOCATION_TERMS = (
    "albania",
    "andorra",
    "armenia",
    "austria",
    "azerbaijan",
    "belarus",
    "belgium",
    "bosnia",
    "bulgaria",
    "croatia",
    "cyprus",
    "czech republic",
    "czechia",
    "denmark",
    "estonia",
    "finland",
    "france",
    "georgia",
    "germany",
    "gibraltar",
    "greece",
    "hungary",
    "iceland",
    "ireland",
    "italy",
    "kazakhstan",
    "kosovo",
    "kyrgyzstan",
    "latvia",
    "liechtenstein",
    "lithuania",
    "luxembourg",
    "malta",
    "moldova",
    "monaco",
    "montenegro",
    "netherlands",
    "north macedonia",
    "norway",
    "poland",
    "portugal",
    "romania",
    "russia",
    "san marino",
    "serbia",
    "slovakia",
    "slovenia",
    "spain",
    "sweden",
    "switzerland",
    "tajikistan",
    "turkey",
    "türkiye",
    "turkmenistan",
    "ukraine",
    "united kingdom",
    "uzbekistan",
    "vatican",
)


@dataclass(frozen=True, slots=True)
class SearchProfile:
    role_keywords: tuple[str, ...] = DEFAULT_ROLE_KEYWORDS
    excluded_title_keywords: tuple[str, ...] = (
        "intern",
        "internship",
        "trainee",
        "junior",
        "entry-level",
        "entry level",
        "graduate",
        "младший",
        "стажёр",
        "стажер",
    )
    preferred_seniority: tuple[str, ...] = (
        "middle",
        "mid-level",
        "mid level",
        "medior",
        "senior",
    )
    focus_qa_keywords: tuple[str, ...] = DEFAULT_FOCUS_QA_KEYWORDS
    domain_keywords: tuple[str, ...] = (
        "fintech",
        "digital asset",
        "crypto",
        "blockchain",
        "web3",
        "artificial intelligence",
        "machine learning",
        "generative ai",
        "genai",
        "saas",
    )
    skill_keywords: tuple[str, ...] = (
        "manual testing",
        "functional testing",
        "regression testing",
        "exploratory testing",
        "api testing",
        "web testing",
        "mobile testing",
        "database testing",
        "sql",
    )
    allowed_locations: tuple[str, ...] = (
        "worldwide",
        "anywhere",
        "global",
        "europe",
        "european union",
        "eu",
        "eu remote",
        "eea",
        "emea",
        "serbia",
        "belgrade",
        "cet",
        "cest",
        "европа",
        "европейский союз",
        "сербия",
        "белград",
        "по всему миру",
        "весь мир",
    )
    blocked_locations: tuple[str, ...] = (
        "us only",
        "usa only",
        "united states only",
        "remote - usa",
        "remote, usa",
        "us-based",
        "us based",
        "authorized to work in the us",
        "eligible to work in the us",
        "reside in the us",
        "within the us",
        "latin america only",
        "latam only",
        "apac only",
        "australia only",
        "india only",
    )
    require_remote: bool = True
    allow_unknown_remote_location: bool = False
    min_score: int = 35


@dataclass(frozen=True, slots=True)
class Board:
    kind: str
    name: str
    token: str
    region: str = "global"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    database_path: Path
    search_profile_path: Path
    job_boards_path: Path
    telegram_channels_path: Path = Path("config/telegram_channels.toml")
    search_interval_minutes: int = 180
    http_timeout_seconds: float = 25.0
    max_search_results: int = 10
    max_review_results: int = 10
    max_alerts_per_run: int = 8
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, require_token: bool = True) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if require_token and not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        try:
            allowed = frozenset(int(value.strip()) for value in raw_ids.split(",") if value.strip())
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs") from exc
        if require_token and not allowed:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain at least one user ID")
        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed,
            database_path=Path(os.getenv("DATABASE_PATH", "data/jobs.sqlite3")),
            search_profile_path=Path(
                os.getenv("SEARCH_PROFILE_PATH", "config/search_profile.toml")
            ),
            job_boards_path=Path(os.getenv("JOB_BOARDS_PATH", "config/job_boards.toml")),
            telegram_channels_path=Path(
                os.getenv("TELEGRAM_CHANNELS_PATH", "config/telegram_channels.toml")
            ),
            search_interval_minutes=max(5, int(os.getenv("SEARCH_INTERVAL_MINUTES", "180"))),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "25")),
            max_search_results=max(1, int(os.getenv("MAX_SEARCH_RESULTS", "10"))),
            max_review_results=max(1, int(os.getenv("MAX_REVIEW_RESULTS", "10"))),
            max_alerts_per_run=max(1, int(os.getenv("MAX_ALERTS_PER_RUN", "8"))),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


def _tuple(data: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key, default)
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"profile.{key} must be a list of strings")
    return tuple(item.casefold().strip() for item in value if item.strip())


def load_profile(path: Path) -> SearchProfile:
    if not path.exists():
        return SearchProfile()
    with path.open("rb") as handle:
        raw = tomllib.load(handle).get("profile", {})
    defaults = SearchProfile()
    return SearchProfile(
        role_keywords=_tuple(raw, "role_keywords", defaults.role_keywords),
        excluded_title_keywords=_tuple(
            raw, "excluded_title_keywords", defaults.excluded_title_keywords
        ),
        preferred_seniority=_tuple(raw, "preferred_seniority", defaults.preferred_seniority),
        focus_qa_keywords=_tuple(raw, "focus_qa_keywords", defaults.focus_qa_keywords),
        domain_keywords=_tuple(raw, "domain_keywords", defaults.domain_keywords),
        skill_keywords=_tuple(raw, "skill_keywords", defaults.skill_keywords),
        allowed_locations=_tuple(raw, "allowed_locations", defaults.allowed_locations),
        blocked_locations=_tuple(raw, "blocked_locations", defaults.blocked_locations),
        require_remote=bool(raw.get("require_remote", defaults.require_remote)),
        allow_unknown_remote_location=bool(
            raw.get("allow_unknown_remote_location", defaults.allow_unknown_remote_location)
        ),
        min_score=int(raw.get("min_score", defaults.min_score)),
    )


def load_boards(path: Path) -> list[Board]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    boards: list[Board] = []
    for kind in ("greenhouse", "lever", "ashby"):
        for item in raw.get(kind, []):
            boards.append(
                Board(
                    kind=kind,
                    name=str(item["name"]),
                    token=str(item["token"]),
                    region=str(item.get("region", "global")),
                    tags=tuple(str(tag) for tag in item.get("tags", [])),
                )
            )
    return boards


def load_telegram_channels(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    with path.open("rb") as handle:
        values = tomllib.load(handle).get("channels", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("channels must be a list of Telegram usernames")
    channels: list[str] = []
    for value in values:
        username = value.removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValueError(f"invalid Telegram channel username: {value}")
        if username.casefold() not in {item.casefold() for item in channels}:
            channels.append(username)
    return tuple(channels)
