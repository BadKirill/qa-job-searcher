from __future__ import annotations

import re

from qa_job_bot.config import EUROPE_CIS_LOCATION_TERMS, SearchProfile
from qa_job_bot.models import Job, MatchResult

REMOTE_WORDS = (
    "remote",
    "distributed",
    "work from home",
    "worldwide",
    "anywhere",
    "telecommute",
)

RESTRICTED_LOCATION_WORDS = (
    "united states",
    "usa",
    "u.s.",
    "latin america",
    "latam",
    "apac",
    "australia",
    "india",
)

OTHER_RESTRICTED_REGIONS = (
    "africa",
    "asia",
    "brazil",
    "canada",
    "china",
    "hong kong",
    "indonesia",
    "israel",
    "japan",
    "malaysia",
    "mena",
    "mexico",
    "middle east",
    "new zealand",
    "north america",
    "philippines",
    "saudi arabia",
    "singapore",
    "south africa",
    "south america",
    "south korea",
    "taiwan",
    "thailand",
    "uae",
    "united arab emirates",
    "vietnam",
)

SERBIA_WORDS = ("serbia", "belgrade", "сербия", "белград")

GEO_CONTEXT_MARKERS = (
    "applicant",
    "based in",
    "candidate",
    "eligible",
    "geograph",
    "hire",
    "hiring",
    "located",
    "location",
    "open to",
    "remote across",
    "remote from",
    "remote in",
    "remote within",
    "reside",
    "timezone",
    "work from",
    "working from",
    "географ",
    "кандидат",
    "локац",
    "нанима",
    "находиться",
    "открыта для",
    "проживать",
    "рассматриваем",
    "удаленно из",
    "удалённо из",
)

HARD_GEO_RESTRICTION_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:must|need to|have to|required to)\b[^.;!?\n]{0,100}"
        r"\b(?:be based|be located|reside|live|work authori[sz]ation)\b[^.;!?\n]{0,100}",
        r"\b(?:based|located|residing|living)\b[^.;!?\n]{0,100}\bonly\b",
        r"\bonly\b[^.;!?\n]{0,80}"
        r"\b(?:applicants?|candidates?|employees?|people|residents?)\b[^.;!?\n]{0,100}",
        r"\b(?:applicants?|candidates?|employees?|people|residents?)\b[^.;!?\n]{0,100}"
        r"\bonly\b",
        r"\b(?:we|the company|the employer)\b[^.;!?\n]{0,40}"
        r"\bonly\b[^.;!?\n]{0,30}\b(?:hire|employ)\b[^.;!?\n]{0,100}",
        r"\b(?:open|available|eligible|restricted|limited)\b[^.;!?\n]{0,40}"
        r"\bonly\b[^.;!?\n]{0,100}",
        r"\b(?:eligible|hiring|remote|work|candidate)\s+"
        r"(?:countries|locations|regions|geograph(?:y|ies))\s*[:\-][^.;!?\n]{1,160}",
        r"\b(?:нужно|необходимо|требуется|должен|должна|должны)\b"
        r"[^.;!?\n]{0,100}\b(?:находиться|проживать|жить)\b[^.;!?\n]{0,100}",
        r"\b(?:только|исключительно)\b[^.;!?\n]{0,80}"
        r"\b(?:кандидат|соискател|резидент)\w*\b[^.;!?\n]{0,100}",
        r"\b(?:кандидат|соискател|резидент)\w*\b[^.;!?\n]{0,100}"
        r"\b(?:только|исключительно)\b",
    )
)

TARGET_EXCLUSION_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:except|excluding)\b[^.;!?\n]{0,80}\b(?:serbia|belgrade)\b",
        r"\b(?:not available|not open|not eligible|cannot hire|can't hire|unable to hire)\b"
        r"[^.;!?\n]{0,80}\b(?:serbia|belgrade)\b",
        r"\b(?:serbia|belgrade)\b[^.;!?\n]{0,60}"
        r"\b(?:excluded|not available|not eligible|not supported)\b",
        r"\b(?:кроме|исключая)\b[^.;!?\n]{0,80}\b(?:сербия|белград)\b",
        r"\b(?:сербия|белград)\b[^.;!?\n]{0,60}"
        r"\b(?:не подходит|недоступна|исключена)\b",
    )
)

JUNIOR_LEVEL_PATTERN = re.compile(
    r"(?<!\w)(?:junior|jr\.?|entry[ -]level|graduate|trainee|intern(?:ship)?)(?!\w)|"
    r"младш\w*|начинающ\w*|стаж[ёе]р\w*",
    re.IGNORECASE,
)

STRUCTURED_JUNIOR_LEVEL_PATTERN = re.compile(
    r"(?:seniority|career level|experience level|job level|уровень|грейд)"
    r"\s*[:\-–—]\s*[^.;!?\n]{0,40}"
    r"(?:junior|jr\.?|entry[ -]level|младш\w*|начинающ\w*)",
    re.IGNORECASE,
)

US_STATE_LOCATION_WORDS = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "florida",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
)


def _contains(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]


def _contains_whole_term(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)]


def _geo_segments(text: str) -> list[str]:
    return [
        normalized
        for value in re.split(r"[\n\r•]+|(?<=[.!?;])\s+", text.casefold())
        if (normalized := re.sub(r"\s+", " ", value).strip())
    ]


def _has_allowed_scope(text: str, profile: SearchProfile) -> bool:
    return bool(_contains_whole_term(text, profile.allowed_locations))


def _has_allowed_description_scope(text: str, profile: SearchProfile) -> bool:
    for segment in _geo_segments(text):
        if _has_allowed_scope(segment, profile) and _contains(segment, GEO_CONTEXT_MARKERS):
            return True
    return False


def _restriction_contexts(text: str) -> list[str]:
    contexts = [
        match.group(0)
        for pattern in HARD_GEO_RESTRICTION_PATTERNS
        for match in pattern.finditer(text)
    ]
    geo_entities = (
        *EUROPE_CIS_LOCATION_TERMS,
        *RESTRICTED_LOCATION_WORDS,
        *OTHER_RESTRICTED_REGIONS,
        *US_STATE_LOCATION_WORDS,
        "сербия",
        "белград",
        "россия",
        "рф",
    )
    for segment in _geo_segments(text):
        has_only = bool(re.search(r"(?<!\w)(?:only|только|исключительно)(?!\w)", segment))
        if has_only and _contains_whole_term(segment, geo_entities):
            contexts.append(segment)
    return contexts


def _has_target_exclusion(text: str) -> bool:
    return any(pattern.search(text) for pattern in TARGET_EXCLUSION_PATTERNS)


def _has_unacceptable_restriction(text: str, profile: SearchProfile) -> bool:
    if _has_target_exclusion(text):
        return True

    for context in _restriction_contexts(text):
        if not _has_allowed_scope(context, profile):
            return True

    for segment in _geo_segments(text):
        if _contains(segment, profile.blocked_locations) and not _has_allowed_scope(
            segment, profile
        ):
            employer_location = re.search(
                r"\b(?:us|usa)[- ]based\s+(?:team|office|company|headquarters)\b", segment
            )
            candidate_restriction = re.search(
                r"\b(?:applicants?|candidates?|must|need to|required|eligible|reside|"
                r"hiring only|hire only)\b",
                segment,
            )
            if employer_location and not candidate_restriction:
                continue
            return True
    return False


def _looks_like_qa_title(title: str, role_keywords: tuple[str, ...]) -> list[str]:
    matches = _contains(title, role_keywords)
    if matches:
        return matches
    if re.search(r"\b(qa|sdet)\b", title):
        return ["qa/sdet"]
    if "quality" in title and any(word in title for word in ("engineer", "engineering")):
        return ["quality engineering"]
    if re.search(
        r"\b(test (automation )?engineer|automation test engineer|test architect)\b", title
    ):
        return ["test engineering"]
    if re.search(
        r"тестировщик|инженер по тестированию|специалист по тестированию|"
        r"руководитель.{0,40}тестирован",
        title,
    ):
        return ["тестирование"]
    return []


def _has_junior_level(job: Job) -> bool:
    if JUNIOR_LEVEL_PATTERN.search(job.title):
        return True
    if any(JUNIOR_LEVEL_PATTERN.fullmatch(tag.strip()) for tag in job.tags):
        return True
    return bool(STRUCTURED_JUNIOR_LEVEL_PATTERN.search(job.description))


def _location_status(job: Job, profile: SearchProfile) -> str:
    location = job.location.casefold()
    description = job.description.casefold()
    location_and_description = f"{location}\n{description}"
    workplace = job.workplace_type.casefold()
    allowed_location = _has_allowed_scope(location, profile)

    if not allowed_location and (
        _contains(location, profile.blocked_locations)
        or _contains_whole_term(location, EUROPE_CIS_LOCATION_TERMS)
        or _contains_whole_term(location, RESTRICTED_LOCATION_WORDS)
        or _contains_whole_term(location, OTHER_RESTRICTED_REGIONS)
        or _contains_whole_term(location, US_STATE_LOCATION_WORDS)
    ):
        return "blocked"
    if _has_unacceptable_restriction(location_and_description, profile):
        return "blocked"

    is_serbia = bool(_contains_whole_term(location, SERBIA_WORDS)) or any(
        _contains_whole_term(segment, SERBIA_WORDS) and _contains(segment, GEO_CONTEXT_MARKERS)
        for segment in _geo_segments(description)
    )
    is_hybrid = (
        workplace == "hybrid"
        or "hybrid" in location
        or bool(
            _contains(
                description,
                (
                    "hybrid role",
                    "hybrid position",
                    "hybrid working",
                    "hybrid work",
                    "гибридная работа",
                    "гибридный формат",
                ),
            )
        )
    )
    if is_hybrid:
        return "allowed" if is_serbia else "not_remote"

    strong_remote_phrases = (
        "fully remote",
        "remote role",
        "remote position",
        "remote-first",
        "100% remote",
        "work remotely",
    )
    is_remote = (
        workplace == "remote"
        or job.remote is True
        or bool(_contains(location, REMOTE_WORDS))
        or bool(_contains(description, strong_remote_phrases))
    )
    if profile.require_remote and not is_remote:
        return "not_remote"

    if allowed_location:
        return "allowed"

    if _has_allowed_description_scope(description, profile):
        return "allowed"

    return "unknown"


def match_job(job: Job, profile: SearchProfile) -> MatchResult:
    title = job.title.casefold()
    searchable = " ".join((job.title, job.description, " ".join(job.tags))).casefold()
    reasons: list[str] = []
    score = 0

    if _contains(title, profile.excluded_title_keywords) or _has_junior_level(job):
        return MatchResult(False, 0, ["исключённый уровень"], "unknown")

    role_matches = _looks_like_qa_title(title, profile.role_keywords)
    if not role_matches:
        return MatchResult(False, 0, ["не QA-позиция"], "unknown")
    score += 35
    reasons.append(f"роль: {role_matches[0]}")

    location_status = _location_status(job, profile)
    if location_status in {"blocked", "not_remote"}:
        return MatchResult(False, score, reasons + ["география не подходит"], location_status)
    if location_status == "allowed":
        score += 25
        reasons.append("подходит по географии")
    elif profile.allow_unknown_remote_location:
        score += 8
        reasons.append("remote, географию стоит проверить")
    else:
        return MatchResult(False, score, reasons + ["география неясна"], location_status)

    seniority = _contains(title, profile.preferred_seniority)
    if seniority:
        score += 15
        reasons.append(f"уровень: {seniority[0]}")

    focus = _contains(searchable, profile.focus_qa_keywords)
    if focus:
        score += 20
        reasons.append(f"фокус: {', '.join(focus[:2])}")

    domains = _contains(searchable, profile.domain_keywords)
    if domains:
        score += min(15, 5 * len(domains))
        reasons.append(f"домен: {', '.join(domains[:2])}")

    skills = _contains(searchable, profile.skill_keywords)
    if skills:
        score += min(15, 3 * len(skills))
        reasons.append(f"навыки: {', '.join(skills[:3])}")

    contract_terms = _contains(searchable, ("contractor", "b2b", "contract role"))
    if contract_terms:
        score += 5
        reasons.append(f"формат: {contract_terms[0]}")

    if job.salary:
        score += 3
        reasons.append("указана зарплата")

    score = min(score, 100)
    return MatchResult(score >= profile.min_score, score, reasons, location_status)


def rank_jobs(jobs: list[Job], profile: SearchProfile) -> list[Job]:
    matched: list[Job] = []
    for job in jobs:
        result = match_job(job, profile)
        if not result.accepted:
            continue
        job.score = result.score
        job.match_reasons = result.reasons
        job.location_status = result.location_status
        matched.append(job)
    return sorted(
        matched,
        key=lambda job: (job.score, job.published_at is not None, job.published_at),
        reverse=True,
    )
