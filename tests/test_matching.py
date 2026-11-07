from dataclasses import replace
from datetime import UTC, datetime

import pytest

from qa_job_bot.config import SearchProfile
from qa_job_bot.matching import match_job, rank_jobs
from qa_job_bot.models import Job


def make_job(**overrides: object) -> Job:
    values: dict[str, object] = {
        "source": "test",
        "source_id": "1",
        "title": "Senior QA Automation Engineer",
        "company": "Example",
        "location": "Remote - Europe",
        "url": "https://example.com/job",
        "description": "Fintech SaaS product. Playwright, Python and API testing.",
        "remote": True,
    }
    values.update(overrides)
    return Job(**values)


def test_accepts_senior_qa_in_europe() -> None:
    result = match_job(make_job(), SearchProfile())
    assert result.accepted
    assert result.location_status == "allowed"
    assert result.score >= 80


def test_rejects_junior_title() -> None:
    result = match_job(make_job(title="Junior QA Engineer"), SearchProfile())
    assert not result.accepted
    assert result.reasons == ["исключённый уровень"]


def test_rejects_combined_junior_middle_title() -> None:
    result = match_job(make_job(title="Junior/Middle QA Automation Engineer"), SearchProfile())
    assert not result.accepted


def test_rejects_russian_junior_title() -> None:
    result = match_job(make_job(title="Младший QA-инженер"), SearchProfile())
    assert not result.accepted


def test_rejects_structured_junior_seniority_in_description() -> None:
    result = match_job(
        make_job(title="QA Engineer", description="Seniority: Junior. Remote across Europe."),
        SearchProfile(),
    )
    assert not result.accepted


def test_accepts_senior_role_that_mentors_juniors() -> None:
    result = match_job(
        make_job(
            title="Senior QA Engineer",
            description="Remote across Europe. Mentor junior engineers and own test automation.",
        ),
        SearchProfile(),
    )
    assert result.accepted


def test_accepts_and_prioritizes_middle_title() -> None:
    result = match_job(make_job(title="Middle QA Engineer"), SearchProfile())
    assert result.accepted
    assert "уровень: middle" in result.reasons


@pytest.mark.parametrize(
    "title,description,term",
    [
        ("Middle Manual QA Engineer", "Regression testing", "manual qa"),
        ("Senior QA Engineer", "Full-stack QA for web and API", "full-stack qa"),
        ("Senior QA Engineer", "Ручное тестирование web и API", "ручное тестирование"),
    ],
)
def test_prioritizes_middle_senior_manual_and_fullstack_qa(
    title: str, description: str, term: str
) -> None:
    focused = match_job(make_job(title=title, description=description), SearchProfile())
    generic = match_job(
        make_job(title="Senior QA Engineer", description="Regression coverage"), SearchProfile()
    )
    assert focused.accepted
    assert focused.score >= generic.score + 20
    assert any(reason.startswith(f"фокус: {term}") for reason in focused.reasons)


def test_focus_terms_are_capped_and_lead_is_not_a_middle_senior_signal() -> None:
    focused = match_job(
        make_job(
            title="QA Lead",
            description="Manual QA, manual testing, and full stack QA for a SaaS product",
        ),
        SearchProfile(),
    )
    assert focused.score == 88
    assert not any(reason.startswith("уровень:") for reason in focused.reasons)
    assert "фокус: manual qa, manual testing" in focused.reasons


def test_rejects_explicit_us_remote() -> None:
    result = match_job(
        make_job(location="United States (Remote)", description="Remote-first fintech"),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "blocked"


def test_worldwide_role_is_not_rejected_by_us_based_team() -> None:
    result = match_job(
        make_job(
            location="Worldwide",
            description="Join our US-based team. We hire worldwide for this remote QA role.",
        ),
        SearchProfile(),
    )
    assert result.accepted
    assert result.location_status == "allowed"


def test_us_based_candidate_restriction_still_blocks_worldwide_label() -> None:
    result = match_job(
        make_job(
            location="Worldwide",
            description="Our US-based team requires candidates to reside in the US.",
        ),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "blocked"


def test_rejects_explicit_canada_remote() -> None:
    result = match_job(
        make_job(location="Canada (Remote)", description="Remote-first fintech"),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "blocked"


def test_rejects_us_state_only_location() -> None:
    result = match_job(
        make_job(location="California (Remote)", description="Remote-first fintech"),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "blocked"


def test_does_not_confuse_lausanne_with_usa() -> None:
    result = match_job(
        make_job(location="Lausanne, Remote", description="Remote-first fintech"),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "unknown"


def test_accepts_serbia_hybrid() -> None:
    result = match_job(
        make_job(
            location="Belgrade, Serbia",
            remote=False,
            workplace_type="hybrid",
        ),
        SearchProfile(),
    )
    assert result.accepted
    assert result.location_status == "allowed"


def test_accepts_serbia_hybrid_when_location_is_in_description() -> None:
    result = match_job(
        make_job(
            location="Hybrid",
            description="Hybrid role. Candidates must be located in Belgrade, Serbia.",
            remote=False,
            workplace_type="hybrid",
        ),
        SearchProfile(),
    )
    assert result.accepted
    assert result.location_status == "allowed"


def test_rejects_hybrid_outside_serbia() -> None:
    result = match_job(
        make_job(location="Poland", remote=False, workplace_type="hybrid"),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "blocked"


def test_rejects_onsite_in_serbia() -> None:
    result = match_job(
        make_job(location="Belgrade, Serbia", remote=False, workplace_type="on-site"),
        SearchProfile(),
    )
    assert not result.accepted
    assert result.location_status == "not_remote"


def test_rejects_non_qa_title_even_if_description_mentions_testing() -> None:
    result = match_job(
        make_job(title="Senior Backend Engineer", description="Own QA and test automation"),
        SearchProfile(),
    )
    assert not result.accepted


def test_rejects_software_engineer_for_test_infrastructure() -> None:
    result = match_job(
        make_job(title="Staff Software Engineer, Developer Infrastructure (Test Infrastructure)"),
        SearchProfile(),
    )
    assert not result.accepted


def test_unknown_remote_can_be_allowed_or_rejected() -> None:
    job = make_job(location="Remote", description="SaaS", remote=True)
    result = match_job(job, SearchProfile())
    assert not result.accepted
    assert result.location_status == "unknown"
    permissive = replace(SearchProfile(), allow_unknown_remote_location=True)
    assert match_job(job, permissive).accepted


def test_accepts_remote_when_full_description_allows_europe() -> None:
    job = make_job(
        location="Remote",
        description="This remote role is open to candidates located anywhere in Europe.",
    )
    result = match_job(job, SearchProfile())
    assert result.accepted
    assert result.location_status == "allowed"


def test_accepts_remote_when_description_allows_emea() -> None:
    job = make_job(
        location="Remote",
        description="We are hiring candidates across EMEA for this remote position.",
    )
    result = match_job(job, SearchProfile())
    assert result.accepted
    assert result.location_status == "allowed"


def test_rejects_country_only_even_when_country_is_in_europe() -> None:
    job = make_job(
        location="Remote - Poland",
        description="This role is remote, but candidates must be located in Poland.",
    )
    result = match_job(job, SearchProfile())
    assert not result.accepted
    assert result.location_status == "blocked"


def test_country_only_location_stays_blocked_in_permissive_mode() -> None:
    job = make_job(location="Poland", description="Remote role.")
    permissive = replace(SearchProfile(), allow_unknown_remote_location=True)
    result = match_job(job, permissive)
    assert not result.accepted
    assert result.location_status == "blocked"


def test_country_only_restriction_late_in_description_overrides_europe() -> None:
    job = make_job(
        location="Remote - Europe",
        description=(
            "Remote-first QA role in Europe. "
            + ("Automation testing and product quality. " * 100)
            + "Candidates must be based in Germany."
        ),
    )
    result = match_job(job, SearchProfile())
    assert not result.accepted
    assert result.location_status == "blocked"


def test_rejects_worldwide_role_that_excludes_serbia() -> None:
    job = make_job(
        location="Worldwide",
        description="Remote worldwide, except Serbia and Belarus.",
    )
    result = match_job(job, SearchProfile())
    assert not result.accepted
    assert result.location_status == "blocked"


def test_accepts_country_list_when_serbia_is_included() -> None:
    job = make_job(
        location="Remote",
        description="We can only hire candidates based in Serbia, Poland, or Germany.",
    )
    result = match_job(job, SearchProfile())
    assert result.accepted
    assert result.location_status == "allowed"


def test_rejects_russian_country_only_restriction() -> None:
    job = make_job(
        location="Remote - Europe",
        description="Рассматриваем только кандидатов, которые проживают в России.",
    )
    result = match_job(job, SearchProfile())
    assert not result.accepted
    assert result.location_status == "blocked"


def test_rejects_non_engineering_quality_manager() -> None:
    job = make_job(
        title="Enablement & Quality Manager - Customer Support",
        description="Manage the customer support quality program",
    )
    assert not match_job(job, SearchProfile()).accepted


def test_accepts_russian_qa_title_from_telegram() -> None:
    job = make_job(
        title="Специалист по тестированию ПО",
        location="Remote · Serbia",
        description="Удалённая работа. Тестирование API.",
        remote=True,
        workplace_type="remote",
    )
    result = match_job(job, SearchProfile())
    assert result.accepted
    assert result.location_status == "allowed"


def test_rank_orders_by_score() -> None:
    lead = make_job(source_id="lead", title="QA Lead", salary="€100k")
    regular = make_job(source_id="regular", title="QA Engineer", description="SaaS")
    assert rank_jobs([regular, lead], SearchProfile())[0].title == "QA Lead"


@pytest.mark.parametrize(
    "title",
    [
        "QA Specialist",
        "Senior Quality Engineer",
        "Senior Automation Test Engineer",
        "Test Architect",
    ],
)
def test_accepts_qa_title_variants_outside_configured_keywords(title: str) -> None:
    profile = replace(SearchProfile(), role_keywords=())
    result = match_job(make_job(title=title), profile)
    assert result.accepted
    assert result.reasons[0].startswith("роль:")


@pytest.mark.parametrize("overrides", [{"title": "Jr. QA Engineer"}, {"tags": ("JUNIOR",)}])
def test_junior_regex_and_structured_tags_cannot_bypass_excluded_terms(overrides: dict) -> None:
    result = match_job(make_job(**overrides), replace(SearchProfile(), excluded_title_keywords=()))
    assert not result.accepted
    assert result.reasons == ["исключённый уровень"]


def test_description_blocklist_overrides_general_remote_location() -> None:
    result = match_job(
        make_job(description="SaaS platform. US-based team required."), SearchProfile()
    )
    assert not result.accepted
    assert result.location_status == "blocked"


def test_hybrid_europe_is_rejected_without_serbia() -> None:
    result = match_job(make_job(location="Europe", workplace_type="hybrid"), SearchProfile())
    assert not result.accepted
    assert result.location_status == "not_remote"


def test_onsite_allowed_when_remote_requirement_explicitly_disabled() -> None:
    profile = replace(SearchProfile(), require_remote=False)
    result = match_job(make_job(location="Serbia", remote=False, description="QA"), profile)
    assert result.accepted


def test_contract_salary_and_score_cap() -> None:
    result = match_job(
        make_job(
            description="fintech crypto saas manual testing functional testing "
            "regression testing exploratory testing api testing contractor",
            salary="100000 EUR",
        ),
        SearchProfile(),
    )
    assert result.score == 100
    assert "формат: contractor" in result.reasons
    assert "указана зарплата" in result.reasons


def test_score_threshold_is_inclusive_and_rank_excludes_rejections() -> None:
    job = make_job(title="QA Engineer", description="")
    assert match_job(job, replace(SearchProfile(), min_score=60)).accepted
    assert not match_job(job, replace(SearchProfile(), min_score=61)).accepted
    older = replace(job, source_id="old", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = replace(job, source_id="new", published_at=datetime(2026, 1, 2, tzinfo=UTC))
    invalid = replace(job, title="Junior QA Engineer")
    ranked = rank_jobs([job, older, invalid, newer], SearchProfile())
    assert [entry.source_id for entry in ranked] == ["new", "old", "1"]
    assert all(entry.location_status == "allowed" and entry.score == 60 for entry in ranked)
    assert rank_jobs([], SearchProfile()) == []
