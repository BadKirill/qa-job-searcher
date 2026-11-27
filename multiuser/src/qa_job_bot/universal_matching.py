import re
from dataclasses import dataclass

from qa_job_bot.config import EUROPE_CIS_LOCATION_TERMS
from qa_job_bot.geography import (
    REGION_MEMBERS,
    country_of,
    covers,
    identified_places,
    mentioned_places,
    preference_covers,
)
from qa_job_bot.matching import (
    GEO_CONTEXT_MARKERS,
    OTHER_RESTRICTED_REGIONS,
    REMOTE_WORDS,
    RESTRICTED_LOCATION_WORDS,
    US_STATE_LOCATION_WORDS,
    _contains,
    _contains_whole_term,
    _geo_segments,
    _restriction_contexts,
)
from qa_job_bot.models import utcnow
from qa_job_bot.work_time import time_conflicts, work_times


def excludes_candidate(text, locations):
    for location in locations:
        term = rf"(?<!\w){re.escape(location)}(?!\w)"
        if re.search(
            r"(?:except|excluding|cannot hire|can't hire|unable to hire|"
            r"not eligible|кроме|исключая)"
            rf"[^.;!?\n]{{0,80}}{term}",
            text,
        ):
            return True
        if re.search(
            rf"{term}[^.;!?\n]{{0,60}}"
            r"(?:excluded|not available|not eligible|not supported|недоступ|исключен)",
            text,
        ):
            return True
    return False


def workplace(job):
    declared = job.workplace_type.casefold().replace("_", "-")
    if declared in {"onsite", "on-site", "office"}:
        return "on-site"
    if declared in {"remote", "hybrid"}:
        return declared
    text = f"{job.location}\n{job.description}".casefold()
    if re.search(r"\bhybrid\b|гибрид", text):
        return "hybrid"
    if (
        job.remote is True
        or _contains(job.location.casefold(), REMOTE_WORDS)
        or re.search(
            r"fully remote|remote role|remote position|remote-first|100% remote|"
            r"work remotely|удал[её]н",
            text,
        )
    ):
        return "remote"
    if job.remote is False or re.search(r"on[ -]?site|from our .*office|office work|офис", text):
        return "on-site"
    return "unknown"


def legacy_location_status(job, profile):
    mode = workplace(job)
    if mode not in profile.workplace_types:
        return "not_remote"
    location = job.location.casefold()
    text = f"{location}\n{job.description}".casefold()
    candidates = profile.candidate_locations
    broad = tuple(
        term
        for term in profile.allowed_locations
        if term
        in {
            "worldwide",
            "anywhere",
            "global",
            "europe",
            "emea",
            "cis",
            "европа",
            "снг",
            "eu",
            "european union",
            "eea",
            "cet",
            "cest",
            "европейский союз",
            "по всему миру",
            "весь мир",
        }
    )
    eligible = tuple(candidates or profile.allowed_locations) + broad
    if excludes_candidate(text, candidates) or any(
        _contains_whole_term(value.casefold(), candidates) for value in job.excluded_locations
    ):
        return "blocked"
    if (
        mode == "hybrid"
        and profile.hybrid_locations
        and not _contains_whole_term(text, profile.hybrid_locations)
    ):
        return "not_remote"
    if (
        job.location_restrictions
        and eligible
        and not any(
            _contains_whole_term(value.casefold(), eligible) for value in job.location_restrictions
        )
    ):
        return "blocked"
    for context in _restriction_contexts(text):
        if eligible and not _contains_whole_term(context, eligible):
            return "blocked"
    for segment in _geo_segments(text):
        if _contains(segment, profile.blocked_locations) and not _contains_whole_term(
            segment, profile.allowed_locations
        ):
            employer = re.search(
                r"\b(?:us|usa)[- ]based\s+(?:team|office|company|headquarters)\b", segment
            )
            restriction = re.search(
                r"applicant|candidate|must|need to|required|eligible|reside|hiring only|hire only",
                segment,
            )
            if not employer or restriction:
                return "blocked"
    if (
        not profile.allowed_locations
        or _contains_whole_term(location, profile.allowed_locations)
        or any(
            _contains_whole_term(value.casefold(), profile.allowed_locations)
            for value in job.location_restrictions
        )
    ):
        return "allowed"
    known = (
        *EUROPE_CIS_LOCATION_TERMS,
        *OTHER_RESTRICTED_REGIONS,
        *RESTRICTED_LOCATION_WORDS,
        *US_STATE_LOCATION_WORDS,
    )
    if _contains_whole_term(location, known):
        return "blocked"
    if any(
        _contains_whole_term(segment, profile.allowed_locations)
        and _contains(segment, GEO_CONTEXT_MARKERS)
        for segment in _geo_segments(job.description)
    ):
        return "allowed"
    return "unknown"


RESTRICTION = re.compile(
    r"\b(?:candidates?|applicants?|you)\b.{0,45}\b(?:reside in|be based in|"
    r"be located in|live in|work from)|\b(?:hiring|hire|eligible)\s+(?:only\s+)?(?:in|from)|"
    r"\b(?:must|need to|required to)\s+(?:currently\s+)?(?:reside in|be based in|"
    r"be located in|live in)|\b(?:только для|прожива\w+ в)",
    re.I,
)
SCOPE_HEADING = re.compile(
    r"^\s*(?:where (?:you can|we hire|you(?:'ll| will)) work|eligible (?:countries|locations)|"
    r"(?:hiring|remote) locations?|applicant location requirements|"
    r"где можно работать|страны найма)\s*[:：]?\s*(.*)$",
    re.I,
)
EXCEPTION = re.compile(
    r"\b(?:except|excluding|cannot hire|can.t hire|unable to hire|"
    r"not eligible|кроме|исключая)\b",
    re.I,
)
ONLY = re.compile(r"\b(?:only|только|исключительно)\b", re.I)
OFFICE_LOCATION = re.compile(
    r"\b(?:office (?:is )?in|based (?:at|in) our office in|офис в)\s+([^.;!\n]+)", re.I
)
TEAM_LOCATION = re.compile(r"\bbased (?:team|office|company|headquarters)\b", re.I)
REMOTE_SCOPE = re.compile(
    r"\b(?:remote(?:ly)? (?:in|across|within|from)|удал[её]нно (?:из|в))\b", re.I
)
BROAD = {"region:europe", "region:emea", "region:mena", "region:latam", "region:apac", "region:cis"}


@dataclass(frozen=True)
class GeographyDecision:
    status: str
    code: str
    reason: str


def employer_scopes(job):
    hard = []
    structured = set().union(*(mentioned_places(value) for value in job.location_restrictions))
    if job.location_restrictions:
        hard.append(structured)
    excluded = set().union(*(mentioned_places(value) for value in job.excluded_locations))
    location, *lines = re.split(r"\n|(?<=[.!?;])\s+", f"{job.location}\n{job.description}")
    listed = False
    list_group = None
    for line in [location, *lines]:
        head = SCOPE_HEADING.match(line)
        exclusion = EXCEPTION.search(line)
        if exclusion:
            tail = line[exclusion.end() :]
            excluded.update(mentioned_places(tail))
            if (
                re.match(r"\s+georgia\b", tail, re.I)
                and "region:world" in mentioned_places(line)
                and "country:US" not in mentioned_places(tail)
            ):
                excluded.add("country:GE")
            line = line[: exclusion.start()]
        if head:
            listed = True
            list_group = mentioned_places(head[1].split("except", 1)[0])
            hard.append(list_group)
            continue
        if listed and line.strip():
            list_places = mentioned_places(line)
            if list_places and not re.search(r"\w.{45}", line) and not RESTRICTION.search(line):
                list_group.update(list_places)
                continue
            listed = False
        if restriction := RESTRICTION.search(line):
            hard.append(mentioned_places(line[restriction.end() :]))
        elif (
            ONLY.search(line) and not TEAM_LOCATION.search(line) or REMOTE_SCOPE.search(line)
        ) and (scope := mentioned_places(line)):
            hard.append(scope)
    return hard, excluded, mentioned_places(EXCEPTION.split(location, maxsplit=1)[0])


def office_decision(job, places):
    offices = mentioned_places(job.location)
    if not offices:
        offices = set().union(
            *(mentioned_places(value) for value in OFFICE_LOCATION.findall(job.description))
        )
    cities = {office for office in offices if office.startswith("city:")}
    if cities:
        offices = cities
    if any(covers(candidate, office) for candidate in places for office in offices):
        return GeographyDecision("allowed", "office_match", f"Офис: {job.location}")
    return GeographyDecision(
        "not_remote",
        "office_mismatch",
        f"Офис: {job.location}" if job.location else "Место офиса не подтверждено",
    )


def geography_decision(job, profile):
    mode = workplace(job)
    candidates = (
        identified_places(profile.candidate_locations)
        if profile.remote_place_ids is None
        else {
            location
            for place in profile.remote_place_ids
            for location in REGION_MEMBERS.get(place, (place,))
        }
    )
    offices = {
        "hybrid": profile.hybrid_place_ids,
        "on-site": profile.office_place_ids,
    }
    if mode in offices:
        selected = offices[mode]
        if selected is not None:
            return office_decision(job, selected)
        if mode == "on-site" and profile.candidate_locations and mode in profile.workplace_types:
            return office_decision(job, candidates)
    if mode not in profile.workplace_types and not (mode == "remote" and candidates):
        return GeographyDecision("not_remote", "workplace_mismatch", "Формат работы не подходит")
    if mode == "remote":
        if profile.remote_place_ids is not None and not profile.remote_place_ids:
            return GeographyDecision(
                "not_remote", "workplace_mismatch", "Удалённая работа отключена"
            )
        hard, excluded, location = employer_scopes(job)
        available = {
            candidate
            for candidate in candidates
            if not any(covers(place, candidate) for place in excluded)
        }
        if candidates and (
            not available
            or excludes_candidate(
                f"{job.location}\n{job.description}".casefold(),
                tuple(item for item in profile.candidate_locations if item != "georgia"),
            )
            and len(candidates) == 1
        ):
            return GeographyDecision(
                "blocked", "employer_exclusion", "Работодатель исключает вашу локацию"
            )
        published_places = location | set().union(*hard)
        if any(
            preference_covers(scope, place)
            for scope in profile.excluded_place_ids
            for place in published_places - {"region:world"}
        ):
            return GeographyDecision(
                "blocked", "preference_exclusion", "Локация исключена в настройках"
            )
        if not hard and location and location != {"region:world"} and candidates:
            if location == {"region:europe"} and not ONLY.search(job.location):
                known = {
                    country_of(candidate)
                    for candidate in candidates
                    if preference_covers("region:europe", candidate)
                    and country_of(candidate) not in {"country:GE", "country:RU"}
                }
                location = {*location, *known}
            hard = [location]
        certain = [
            (group - BROAD - {"region:world"}) or (group - BROAD)
            for group in hard
            if group - BROAD or not group & BROAD
        ]
        broad = any(group & BROAD and not group - BROAD for group in hard)
        if certain and not available and any(group != {"region:world"} for group in certain):
            return GeographyDecision(
                "unknown", "candidate_location_missing", "Укажите фактическую локацию"
            )
        checked = available
        for group in certain:
            if not group:
                broad = True
                continue
            checked = {
                candidate
                for candidate in checked
                if any(covers(scope, candidate) for scope in group)
            }
            if not checked and (available or group != {"region:world"}):
                is_eu = group == {"region:eu"}
                return GeographyDecision(
                    "blocked",
                    "eu_only" if is_eu else "hiring_location_mismatch",
                    "Нанимают только из ЕС" if is_eu else "Локация вне списка стран найма",
                )
        if broad and not any(group - REGION_MEMBERS.keys() - {"region:world"} for group in certain):
            return GeographyDecision("unknown", "geography_unconfirmed", "Проверьте географию")
        preference_matches = checked if published_places == {"region:world"} else published_places
        if (
            profile.preferred_place_ids
            and preference_matches
            and not any(
                preference_covers(preferred, place)
                for preferred in profile.preferred_place_ids
                for place in preference_matches
            )
        ):
            return GeographyDecision(
                "blocked", "geography_preference", "Локация вне выбранных предпочтений"
            )
        if certain or checked and location or location == {"region:world"}:
            return GeographyDecision("allowed", "hiring_location_match", "Подходит по географии")
        if profile.remote_place_ids is not None or candidates:
            return GeographyDecision("unknown", "geography_unconfirmed", "Проверьте географию")
    status = legacy_location_status(job, profile)
    return GeographyDecision(
        status,
        f"geography_{status}",
        {
            "blocked": "География не подходит",
            "not_remote": "Формат или место работы не подходит",
            "unknown": "Проверьте географию",
            "allowed": "Подходит по географии",
        }[status],
    )


def location_decision(job, profile, day=None):
    decision = geography_decision(job, profile)
    if decision.status in {"allowed", "unknown"} and any(
        time_conflicts(requirement, profile.availability, day or utcnow().date())
        for requirement in work_times(job.description)
    ):
        return GeographyDecision(
            "blocked", "working_hours_conflict", "Обязательные часы вне вашей доступности"
        )
    return decision


def location_status(job, profile):
    return location_decision(job, profile).status
