import re

from qa_job_bot.config import EUROPE_CIS_LOCATION_TERMS
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


def location_status(job, profile):
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
