import re
import unicodedata
from dataclasses import asdict
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qa_job_bot.config import SearchProfile
from qa_job_bot.geography import PLACES, preference_covers

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


class Availability(StrictInput):
    zone: Annotated[str, Field(min_length=1, max_length=60)]
    start: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
    end: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]

    @field_validator("zone")
    @classmethod
    def known_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Выберите часовой пояс IANA") from None
        return value

    @model_validator(mode="after")
    def nonempty_window(self):
        if self.start == self.end:
            raise ValueError("Начало и конец доступности должны различаться")
        return self


class SearchSpec(StrictInput):
    remote_place_ids: Terms | None = None
    hybrid_place_ids: Terms | None = None
    office_place_ids: Terms | None = None
    preferred_place_ids: Terms = []
    excluded_place_ids: Terms = []
    availability: Availability | None = None

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

    @field_validator(
        "remote_place_ids",
        "hybrid_place_ids",
        "office_place_ids",
        "preferred_place_ids",
        "excluded_place_ids",
    )
    @classmethod
    def place_ids(cls, values, info):
        if values is None:
            return None
        for value in values:
            if value not in PLACES:
                raise ValueError("Выберите локацию из подсказок")
            if info.field_name in {"hybrid_place_ids", "office_place_ids"} and (
                PLACES[value].kind not in {"city", "country"}
            ):
                raise ValueError("Выберите город или страну")
            if info.field_name == "remote_place_ids" and value == "region:world":
                raise ValueError("Выберите фактическую локацию")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def consistent_places(self):
        if any(
            preference_covers(excluded, preferred)
            for excluded in self.excluded_place_ids
            for preferred in self.preferred_place_ids
        ):
            raise ValueError("Предпочтения и исключения по географии пересекаются")
        return self

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
            if "handle" not in value or set(value) - {"handle", "tags"}:
                raise ValueError("Укажите только handle публичного канала")
            handle = clean_text(value["handle"]).removeprefix("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", handle):
                raise ValueError("Нужен handle канала, а не URL")
            result = {"handle": handle}
            if "tags" in value:
                result["tags"] = source_tags(value)
            return result
        if set(value) - {"name", "token", "region", "tags"} or "token" not in value:
            raise ValueError("Недопустимые параметры доски")
        token = clean_text(value["token"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", token):
            raise ValueError("Нужен идентификатор доски, а не URL")
        region = value.get("region", "global")
        if region not in ({"global", "eu"} if self.kind == "lever" else {"global"}):
            raise ValueError("Недопустимый регион API")
        return {
            "name": clean_text(value.get("name", token)),
            "token": token,
            "region": region,
            "tags": source_tags(value),
        }


def source_tags(value):
    tags = value.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 12:
        raise ValueError("Не более 12 тегов")
    return list(dict.fromkeys(clean_text(tag) for tag in tags))


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


def profession_template(label, roles, focus, skills, *, qa_title_fallback=False):
    return label, SearchSpec(
        role_keywords=roles,
        source_queries=roles[:4],
        focus_qa_keywords=focus,
        skill_keywords=skills,
        qa_title_fallback=qa_title_fallback,
    ).model_dump()


def search_examples():
    templates = {
        "qa": profession_template(
            "QA · общий шаблон",
            ["qa engineer", "quality assurance engineer", "software tester", "тестировщик"],
            ["manual testing", "test automation", "api testing", "regression testing"],
            ["test design", "test cases", "bug reporting", "postman", "sql", "jira", "testrail"],
            qa_title_fallback=True,
        ),
        "manual_qa": profession_template(
            "Manual QA",
            ["manual qa", "manual tester", "manual test engineer", "ручной тестировщик"],
            ["exploratory testing", "regression testing", "functional testing", "api testing"],
            [
                "test design",
                "checklists",
                "bug reporting",
                "postman",
                "devtools",
                "sql",
                "jira",
                "testrail",
            ],
        ),
        "qa_automation": profession_template(
            "QA Automation / SDET",
            [
                "qa automation",
                "test automation engineer",
                "sdet",
                "software development engineer in test",
                "инженер по автоматизации тестирования",
            ],
            ["test automation", "end-to-end testing", "api testing", "test frameworks"],
            [
                "playwright",
                "selenium",
                "cypress",
                "pytest",
                "junit",
                "allure",
                "github actions",
                "docker",
            ],
        ),
        "developer": profession_template(
            "Backend и разработка ПО",
            ["backend engineer", "software engineer", "backend developer", "бэкенд-разработчик"],
            ["rest api", "microservices", "distributed systems", "backend development"],
            [
                "python",
                "java",
                "golang",
                "node.js",
                "postgresql",
                "redis",
                "docker",
                "git",
                "fastapi",
            ],
        ),
        "frontend": profession_template(
            "Frontend-разработка",
            [
                "frontend engineer",
                "frontend developer",
                "front-end developer",
                "фронтенд-разработчик",
            ],
            ["web accessibility", "web performance", "design systems", "responsive design"],
            [
                "typescript",
                "javascript",
                "react",
                "vue.js",
                "angular",
                "next.js",
                "html",
                "css",
                "vite",
                "playwright",
            ],
        ),
        "fullstack": profession_template(
            "Full-stack-разработка",
            [
                "full stack engineer",
                "full-stack developer",
                "fullstack developer",
                "фулстек-разработчик",
            ],
            ["full-stack development", "rest api", "system design", "web development"],
            ["typescript", "react", "next.js", "node.js", "python", "postgresql", "docker", "git"],
        ),
        "mobile": profession_template(
            "Мобильная разработка",
            [
                "mobile engineer",
                "ios developer",
                "android developer",
                "flutter developer",
                "react native developer",
                "мобильный разработчик",
            ],
            ["mobile development", "mobile architecture", "app performance", "mobile applications"],
            [
                "swift",
                "swiftui",
                "kotlin",
                "jetpack compose",
                "flutter",
                "react native",
                "xcode",
                "android studio",
            ],
        ),
        "designer": profession_template(
            "Продуктовый и UX/UI-дизайн",
            ["product designer", "ux designer", "ui/ux designer", "продуктовый дизайнер"],
            ["design systems", "interaction design", "user flows", "ux research"],
            [
                "figma",
                "figjam",
                "prototyping",
                "wireframing",
                "usability testing",
                "accessibility",
                "maze",
            ],
        ),
        "ux_research": profession_template(
            "UX-исследования",
            ["ux researcher", "user researcher", "design researcher", "ux-исследователь"],
            ["user research", "generative research", "qualitative research", "research synthesis"],
            [
                "user interviews",
                "usability testing",
                "survey design",
                "dovetail",
                "maze",
                "figjam",
                "miro",
            ],
        ),
        "data_analyst": profession_template(
            "Аналитика данных и продукта",
            [
                "data analyst",
                "product analyst",
                "bi analyst",
                "аналитик данных",
                "продуктовый аналитик",
            ],
            ["product analytics", "business intelligence", "experimentation", "data visualization"],
            ["sql", "python", "excel", "tableau", "power bi", "looker", "amplitude", "a/b testing"],
        ),
        "business_analyst": profession_template(
            "Бизнес- и системный анализ",
            [
                "business analyst",
                "systems analyst",
                "business systems analyst",
                "бизнес-аналитик",
                "системный аналитик",
            ],
            [
                "requirements analysis",
                "business processes",
                "business analysis",
                "systems analysis",
            ],
            ["bpmn", "uml", "sql", "jira", "confluence", "miro", "user stories", "openapi"],
        ),
        "data_engineer": profession_template(
            "Data Engineering",
            ["data engineer", "analytics engineer", "etl developer", "инженер данных"],
            ["data pipelines", "data modeling", "data warehouse", "stream processing"],
            ["sql", "python", "dbt", "airflow", "spark", "kafka", "bigquery", "snowflake"],
        ),
        "devops": profession_template(
            "DevOps / SRE",
            [
                "devops engineer",
                "site reliability engineer",
                "platform engineer",
                "cloud infrastructure engineer",
            ],
            ["infrastructure as code", "observability", "incident response", "continuous delivery"],
            [
                "linux",
                "terraform",
                "kubernetes",
                "docker",
                "ansible",
                "prometheus",
                "grafana",
                "github actions",
            ],
        ),
        "product_manager": profession_template(
            "Product Management",
            ["product manager", "product owner", "продакт-менеджер", "менеджер продукта"],
            ["product discovery", "product strategy", "roadmap", "experimentation"],
            [
                "user interviews",
                "product metrics",
                "prioritization",
                "jira",
                "amplitude",
                "mixpanel",
                "figma",
            ],
        ),
    }
    return {
        "items": {key: profile for key, (_, profile) in templates.items()},
        "labels": {key: label for key, (label, _) in templates.items()},
    }
