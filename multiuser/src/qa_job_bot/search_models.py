import re
import unicodedata
from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qa_job_bot.config import SearchProfile

KINDS = (
    "remotive",
    "himalayas",
    "weworkremotely",
    "remote_com",
    "hirify",
    "greenhouse",
    "lever",
    "ashby",
    "telegram",
)
AGGREGATORS = KINDS[:5]
Kind = Literal[
    "remotive",
    "himalayas",
    "weworkremotely",
    "remote_com",
    "hirify",
    "greenhouse",
    "lever",
    "ashby",
    "telegram",
]
Terms = Annotated[list[str], Field(max_length=40)]


def clean_text(value):
    if not isinstance(value, str):
        raise ValueError("Ожидается текст")
    value = unicodedata.normalize("NFKC", value).strip()
    if (
        not value
        or len(value) > 120
        or any(ord(c) < 32 or unicodedata.category(c) == "Cs" for c in value)
    ):
        raise ValueError("Недопустимый текст")
    return value


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchSpec(StrictInput):
    role_keywords: Annotated[list[str], Field(min_length=1, max_length=40)]
    source_queries: Annotated[list[str], Field(max_length=8)] = []
    excluded_title_keywords: Terms = []
    preferred_seniority: Terms = []
    focus_qa_keywords: Terms = []
    domain_keywords: Terms = []
    skill_keywords: Terms = []
    allowed_locations: Terms = []
    blocked_locations: Terms = []
    candidate_locations: Terms = []
    hybrid_locations: Terms = []
    workplace_types: Annotated[
        list[Literal["remote", "hybrid", "on-site"]], Field(min_length=1, max_length=3)
    ] = ["remote"]
    working_languages: Annotated[list[Literal["ru", "en"]], Field(max_length=2)] = []
    exclude_entry_level: bool = False
    qa_title_fallback: bool = False
    allow_unknown_remote_location: bool = False
    min_score: Annotated[int, Field(ge=0, le=100)] = 35
    matching_mode: Literal["universal"] = "universal"

    @field_validator(
        "role_keywords",
        "source_queries",
        "excluded_title_keywords",
        "preferred_seniority",
        "focus_qa_keywords",
        "domain_keywords",
        "skill_keywords",
        "allowed_locations",
        "blocked_locations",
        "candidate_locations",
        "hybrid_locations",
    )
    @classmethod
    def terms(cls, values):
        return list(dict.fromkeys(clean_text(item).casefold() for item in values))

    @field_validator("workplace_types")
    @classmethod
    def modes(cls, values):
        return list(dict.fromkeys(values))

    @field_validator("working_languages")
    @classmethod
    def languages(cls, values):
        return sorted(set(values))

    def runtime(self):
        data = self.model_dump()
        return SearchProfile(
            **{
                key: tuple(value) if isinstance(value, list) else value
                for key, value in data.items()
            }
        )


class SourceSpec(StrictInput):
    kind: Kind
    enabled: bool = True
    config: dict = {}

    def normalized(self):
        value = self.config
        if self.kind in AGGREGATORS:
            if value:
                raise ValueError("У агрегатора нет параметров подключения")
            return {}
        if self.kind == "telegram":
            if set(value) != {"handle"}:
                raise ValueError("Укажите только handle публичного канала")
            handle = clean_text(value["handle"]).removeprefix("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", handle):
                raise ValueError("Нужен handle канала, а не URL")
            return {"handle": handle}
        if set(value) - {"name", "token", "region", "tags"} or "token" not in value:
            raise ValueError("Недопустимые параметры доски")
        token = clean_text(value["token"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", token):
            raise ValueError("Нужен идентификатор доски, а не URL")
        region = value.get("region", "global")
        if region not in ({"global", "eu"} if self.kind == "lever" else {"global"}):
            raise ValueError("Недопустимый регион API")
        tags = value.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 12:
            raise ValueError("Не более 12 тегов")
        return {
            "name": clean_text(value.get("name", token)),
            "token": token,
            "region": region,
            "tags": list(dict.fromkeys(clean_text(tag) for tag in tags)),
        }


def source_identity(kind, config):
    if kind in AGGREGATORS:
        return kind
    if kind == "telegram":
        return kind + ":" + config["handle"].casefold()
    return f"{kind}:{config.get('region', 'global')}:{config['token'].casefold()}"


def universal_profile(data):
    if data.get("matching_mode") == "universal":
        return SearchSpec.model_validate(data).model_dump()
    previous = SearchProfile(
        **{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in data.items()
            if key in SearchProfile.__dataclass_fields__
        }
    )
    result = asdict(previous)
    result.update(
        matching_mode="universal",
        workplace_types=["remote", "hybrid"]
        if previous.require_remote
        else ["remote", "hybrid", "on-site"],
        hybrid_locations=["serbia", "belgrade", "сербия", "белград"],
        candidate_locations=["serbia", "belgrade", "сербия", "белград"],
    )
    result.pop("require_remote")
    return SearchSpec.model_validate(
        {key: list(value) if isinstance(value, tuple) else value for key, value in result.items()}
    ).model_dump()


def search_presets():
    qa = universal_profile(asdict(SearchProfile()))
    developer = SearchSpec(
        role_keywords=["backend engineer", "software engineer"],
        skill_keywords=["python"],
        allowed_locations=["worldwide", "europe"],
    ).model_dump()
    designer = SearchSpec(
        role_keywords=["product designer", "ux designer"],
        skill_keywords=["figma"],
        allowed_locations=["europe", "germany", "berlin"],
        candidate_locations=["germany", "berlin"],
        workplace_types=["remote", "hybrid", "on-site"],
    ).model_dump()
    return {"qa": qa, "developer": developer, "designer": designer}
