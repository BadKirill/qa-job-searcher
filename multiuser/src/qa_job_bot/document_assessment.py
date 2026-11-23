from typing import Annotated

from pydantic import Field, ValidationError

from qa_job_bot.document_policy import PolicyViolation, Review, StrictModel, Text


class QualityCheck(StrictModel):
    passed: bool
    issues: Annotated[list[Text], Field(max_length=20)]


class DocumentAssessment(StrictModel):
    vacancy_alignment: QualityCheck
    evidence_selection: QualityCheck
    document_structure: QualityCheck


class AssessedReview(Review):
    assessment: DocumentAssessment


def assess_review(response: dict) -> tuple[dict, dict]:
    try:
        parsed = AssessedReview.model_validate(response).model_dump()
    except ValidationError as error:
        raise PolicyViolation("Reviewer did not provide a valid document assessment") from error
    assessment = parsed.pop("assessment")
    issues = []
    for name, check in assessment.items():
        if check["passed"] == bool(check["issues"]):
            raise PolicyViolation("Reviewer returned a contradictory document assessment")
        issues.extend(f"{name}: {issue}" for issue in check["issues"])
    if issues:
        if parsed["verdict"] == "pass":
            parsed["verdict"] = "revise"
        parsed["issues"] = list(dict.fromkeys(parsed["issues"] + issues))
    return parsed, assessment
