import copy

import pytest

from qa_job_bot.document_policy import (
    PolicyViolation,
    canonical_text,
    content_hash,
    validate_draft,
    validate_review,
)


def context():
    return {
        "kind": "cv",
        "language": "en",
        "template": {
            "facts": [
                {
                    "id": "f1",
                    "text": "Alex has 3 years QA experience since 2023. alex@example.com https://example.com",
                }
            ]
        },
    }


def draft():
    return {
        "title": "Alex",
        "language": "en",
        "sections": [
            {
                "heading": "Experience",
                "paragraphs": [
                    {"text": "Alex has 3 years QA experience since 2023.", "fact_ids": ["f1"]}
                ],
            }
        ],
    }


def review(document):
    return {
        "verdict": "pass",
        "issues": [],
        "draft_hash": content_hash(document),
        "checked_fact_ids": ["f1"],
    }


def test_valid_document_and_review():
    document = validate_draft(draft(), context())
    assert validate_review(review(document), document, context())["verdict"] == "pass"
    assert (
        canonical_text(document) == "Alex\nExperience\nAlex has 3 years QA experience since 2023."
    )
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


@pytest.mark.parametrize(
    "change",
    [
        {"approved": True},
        {"sections": []},
        {"language": "ru"},
        {"title": " "},
        {"title": "Alex\x00"},
        {"title": "Alex\u202e"},
        {"language": 4},
    ],
)
def test_document_structure_language_and_controls(change):
    document = draft() | change
    with pytest.raises(PolicyViolation):
        validate_draft(document, context())


@pytest.mark.parametrize(
    "text,ids",
    [
        ("Alex has 10 years QA experience.", ["f1"]),
        ("alex@attacker.com", ["f1"]),
        ("https://attacker.com", ["f1"]),
        ("QA", ["f9"]),
        ("QA", ["f1", "f1"]),
        ("QA", []),
        (" ", ["f1"]),
        ("QA\u200b", ["f1"]),
    ],
)
def test_unsupported_claims_and_evidence(text, ids):
    document = draft()
    document["sections"][0]["paragraphs"][0] = {"text": text, "fact_ids": ids}
    with pytest.raises(PolicyViolation):
        validate_draft(document, context())


@pytest.mark.parametrize(
    "facts",
    [
        [],
        [{"id": "x", "text": "QA", "confirmed": False}],
        [{"id": "x", "text": "QA"}, {"id": "x", "text": "QA"}],
    ],
)
def test_no_confirmed_or_duplicate_facts(facts):
    source = context()
    source["template"]["facts"] = facts
    with pytest.raises(PolicyViolation):
        validate_draft(draft(), source)


def test_no_template_and_oversized_document():
    with pytest.raises(PolicyViolation):
        validate_draft(draft(), {})
    document = draft()
    paragraph = {"text": "QA " * 1900, "fact_ids": ["f1"]}
    document["sections"][0]["paragraphs"] = [copy.deepcopy(paragraph) for _ in range(8)]
    with pytest.raises(PolicyViolation, match="too long"):
        validate_draft(document, context())


def test_language_and_empty_heading_and_allowed_contacts():
    document = draft()
    document["language"] = "ru"
    document["sections"][0]["heading"] = ""
    document["sections"][0]["paragraphs"][0]["text"] = "alex@example.com https://example.com"
    source = context() | {"kind": "cover_letter", "language": "ru"}
    assert validate_draft(document, source)["language"] == "ru"
    assert canonical_text(document).startswith("Alex\nalex@example.com")


@pytest.mark.parametrize(
    "change",
    [
        {"verdict": "approved"},
        {"approved": True},
        {"draft_hash": "0" * 64},
        {"checked_fact_ids": ["f2"]},
        {"checked_fact_ids": ["f1", "f1"]},
        {"checked_fact_ids": []},
        {"issues": ["Contradiction"]},
        {"verdict": "revise"},
    ],
)
def test_reviewer_fails_closed(change):
    document = draft()
    with pytest.raises(PolicyViolation):
        validate_review(review(document) | change, document, context())


@pytest.mark.parametrize("verdict", ["revise", "needs_input"])
def test_reviewer_explains_required_input(verdict):
    document = draft()
    response = review(document) | {"verdict": verdict, "issues": ["Confirm candidate role"]}
    assert validate_review(response, document, context())["verdict"] == verdict
