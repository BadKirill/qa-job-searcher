import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Text = Annotated[str, Field(min_length=1, max_length=6000, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=120, strict=True)]


class PolicyViolation(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Paragraph(StrictModel):
    text: Text
    fact_ids: Annotated[list[Identifier], Field(min_length=1, max_length=30)]
    kind: Literal["body", "bullet", "entry", "role", "contact"] = "body"


class Section(StrictModel):
    heading: Annotated[str, Field(max_length=120, strict=True)]
    paragraphs: Annotated[list[Paragraph], Field(min_length=1, max_length=200)]


class Draft(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=160, strict=True)]
    language: Annotated[str, Field(min_length=2, max_length=24, strict=True)]
    sections: Annotated[list[Section], Field(min_length=1, max_length=16)]


class Review(StrictModel):
    verdict: Literal["pass", "revise", "needs_input"]
    issues: Annotated[list[Text], Field(max_length=100)]
    draft_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    checked_fact_ids: Annotated[list[Identifier], Field(max_length=500)]


class VisualReview(StrictModel):
    verdict: Literal["pass", "revise"]
    issues: Annotated[list[Text], Field(max_length=100)]
    template_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    rendered_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    draft_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def content_hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def canonical_text(draft: dict) -> str:
    parts = [draft["title"]]
    for section in draft["sections"]:
        if section["heading"]:
            parts.append(section["heading"])
        parts.extend(paragraph["text"] for paragraph in section["paragraphs"])
    return "\n".join(parts)


def _safe_text(text: str) -> None:
    if not text.strip() or any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise PolicyViolation("Empty text or forbidden control characters")
    if re.search(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]", text):
        raise PolicyViolation("Hidden or directional control characters")


def validate_draft(draft: dict, context: dict) -> dict:
    try:
        result = Draft.model_validate(draft).model_dump(exclude_defaults=True)
    except ValidationError as error:
        raise PolicyViolation("Invalid document structure") from error
    template = context.get("template") or {}
    facts = template.get("facts", [])
    if not facts or any(fact.get("confirmed") is False for fact in facts):
        raise PolicyViolation("Confirmed candidate facts are required")
    fact_map = {fact["id"]: fact["text"] for fact in facts}
    if len(fact_map) != len(facts):
        raise PolicyViolation("Duplicate fact identifiers")
    language = "en" if context.get("kind") == "cv" else context.get("language", "en")
    if result["language"].casefold() != language.casefold():
        raise PolicyViolation("Document language does not match the task")
    if len(canonical_text(result)) > 45000:
        raise PolicyViolation("Document is too long")
    _safe_text(result["title"])
    for section in result["sections"]:
        if section["heading"]:
            _safe_text(section["heading"])
        for paragraph in section["paragraphs"]:
            _safe_text(paragraph["text"])
            ids = paragraph["fact_ids"]
            if len(set(ids)) != len(ids) or not set(ids).issubset(fact_map):
                raise PolicyViolation("Missing, duplicate or fabricated evidence references")
            evidence = " ".join(fact_map[fact_id] for fact_id in ids)
            numeric = r"(?<![\w])\d+(?:[.,]\d+)?(?![\w])"
            if not set(re.findall(numeric, paragraph["text"])) <= set(
                re.findall(numeric, evidence)
            ):
                raise PolicyViolation("Document contains unsupported numbers or dates")
            contacts = r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|https?://[^\s)>,]+"
            if not set(re.findall(contacts, paragraph["text"])) <= set(
                re.findall(contacts, evidence)
            ):
                raise PolicyViolation("Document contains unsupported contact information")
    return result


def validate_visual_review(
    review: dict, draft: dict, template_reference: dict, rendered_reference: dict
) -> dict:
    try:
        result = VisualReview.model_validate(review).model_dump()
    except ValidationError as error:
        raise PolicyViolation("Invalid visual review") from error
    if (
        result["template_sha256"] != template_reference["sha256"]
        or result["rendered_sha256"] != rendered_reference["sha256"]
        or result["draft_hash"] != content_hash(draft)
    ):
        raise PolicyViolation("Visual review belongs to different files")
    if result["verdict"] != "pass" or result["issues"]:
        raise PolicyViolation("Проверка оформления не пройдена: " + "; ".join(result["issues"]))
    return result


def validate_review(review: dict, draft: dict, context: dict) -> dict:
    checked = validate_draft(draft, context)
    try:
        result = Review.model_validate(review).model_dump()
    except ValidationError as error:
        raise PolicyViolation("Reviewer returned an invalid response") from error
    if result["draft_hash"] != content_hash(checked):
        raise PolicyViolation("Review belongs to a different document")
    used_ids = {
        fact_id
        for section in checked["sections"]
        for paragraph in section["paragraphs"]
        for fact_id in paragraph["fact_ids"]
    }
    checked_ids = result["checked_fact_ids"]
    known_ids = {fact["id"] for fact in context["template"]["facts"]}
    if len(checked_ids) != len(set(checked_ids)) or not set(checked_ids) <= known_ids:
        raise PolicyViolation("Reviewer evidence is invalid")
    if result["verdict"] == "pass" and (result["issues"] or not used_ids <= set(checked_ids)):
        raise PolicyViolation("Reviewer did not verify every assertion")
    if result["verdict"] != "pass" and not result["issues"]:
        raise PolicyViolation("Reviewer must explain required changes")
    return result
