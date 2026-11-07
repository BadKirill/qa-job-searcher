from copy import deepcopy

import pytest

from qa_job_bot.document_assessment import AssessedReview, assess_review
from qa_job_bot.document_policy import PolicyViolation


def assessment_response():
    return {
        "verdict": "pass",
        "issues": [],
        "draft_hash": "a" * 64,
        "checked_fact_ids": ["identity", "automation"],
        "assessment": {
            name: {"passed": True, "issues": []}
            for name in ("vacancy_alignment", "evidence_selection", "document_structure")
        },
    }


def test_complete_assessment_preserves_review_and_separates_audit():
    response = assessment_response()
    before = deepcopy(response)
    review, assessment = assess_review(response)
    assert review == {key: value for key, value in response.items() if key != "assessment"}
    assert assessment == response["assessment"]
    assert response == before
    schema = AssessedReview.model_json_schema()
    assert "assessment" in schema["required"]
    assert set(schema["$defs"]["DocumentAssessment"]["required"]) == set(assessment)


@pytest.mark.parametrize("dimension", list(assessment_response()["assessment"]))
@pytest.mark.parametrize("verdict", ["pass", "revise", "needs_input"])
def test_quality_failure_cannot_be_overruled_by_factual_pass(dimension, verdict):
    response = assessment_response()
    response["verdict"] = verdict
    response["assessment"][dimension] = {
        "passed": False,
        "issues": ["Lead with the supported TypeScript automation work."],
    }
    review, assessment = assess_review(response)
    assert review["verdict"] == ("needs_input" if verdict == "needs_input" else "revise")
    assert review["issues"] == [f"{dimension}: Lead with the supported TypeScript automation work."]
    assert assessment[dimension]["passed"] is False


@pytest.mark.parametrize(
    "check",
    [
        {"passed": True, "issues": ["Generic leadership pitch"]},
        {"passed": False, "issues": []},
        {"passed": "true", "issues": []},
        {"passed": True},
        {"passed": True, "issues": [], "approved": True},
    ],
)
def test_invalid_or_contradictory_quality_checks_fail_closed(check):
    response = assessment_response()
    response["assessment"]["vacancy_alignment"] = check
    with pytest.raises(PolicyViolation):
        assess_review(response)


@pytest.mark.parametrize("missing", ["assessment", "evidence_selection"])
def test_missing_quality_check_is_not_implicit_approval(missing):
    response = assessment_response()
    if missing == "assessment":
        response.pop(missing)
    else:
        response["assessment"].pop(missing)
    with pytest.raises(PolicyViolation, match="assessment"):
        assess_review(response)


def test_factual_rejection_remains_rejected_when_all_quality_checks_pass():
    response = assessment_response()
    response.update(verdict="revise", issues=["Java experience is unsupported."])
    review, _ = assess_review(response)
    assert review["verdict"] == "revise"
    assert review["issues"] == ["Java experience is unsupported."]


def test_multiple_failed_dimensions_preserve_factual_issues_and_deduplicate():
    response = assessment_response()
    response["issues"] = ["Unsupported Java.", "vacancy_alignment: Generic summary"]
    response["assessment"]["vacancy_alignment"] = {
        "passed": False,
        "issues": ["Generic summary"],
    }
    response["assessment"]["document_structure"] = {
        "passed": False,
        "issues": ["Missing skills section"],
    }
    review, _ = assess_review(response)
    assert review["issues"] == [
        "Unsupported Java.",
        "vacancy_alignment: Generic summary",
        "document_structure: Missing skills section",
    ]
