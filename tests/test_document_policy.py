import copy

import pytest

from qa_job_bot.document_policy import (
    PolicyViolation,
    bind_visual_review,
    canonical_text,
    content_hash,
    template_visual_reference,
    validate_draft,
    validate_review,
    validate_visual_review,
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


def test_employer_name_cannot_be_substituted_with_valid_fact_id():
    source = context()
    source["template"]["facts"][0]["text"] = "Alex worked at Acme as QA Engineer."
    document = draft()
    document["sections"][0]["paragraphs"][0]["text"] = "Alex worked at Globex as QA Engineer."
    with pytest.raises(PolicyViolation, match="Employer name"):
        validate_draft(document, source)
    document["sections"][0]["paragraphs"][0]["text"] = "Alex worked at Acme as QA Engineer."
    assert validate_draft(document, source)["sections"][0]["paragraphs"][0]["text"] == (
        "Alex worked at Acme as QA Engineer."
    )


def test_role_line_and_confirmed_identity_are_immutable():
    source = context()
    source["template"]["facts"] = [
        {"id": "name", "kind": "title", "text": "Alex Example"},
        {"id": "role", "kind": "role", "text": "QA Engineer at Acme"},
    ]
    document = {
        "title": "Alex Example",
        "language": "en",
        "sections": [
            {
                "heading": "Experience",
                "paragraphs": [
                    {"text": "QA Engineer at Acme", "fact_ids": ["role"], "kind": "role"}
                ],
            }
        ],
    }
    assert validate_draft(document, source)["title"] == "Alex Example"
    document["sections"][0]["paragraphs"][0]["text"] = "QA Manager at Acme"
    with pytest.raises(PolicyViolation, match="Employment or role"):
        validate_draft(document, source)
    document["sections"][0]["paragraphs"][0]["text"] = "QA Engineer at Acme"
    document["title"] = "Alex Someone"
    with pytest.raises(PolicyViolation, match="candidate identity"):
        validate_draft(document, source)
    document["title"] = "Curriculum Vitae"
    assert validate_draft(document, source)["title"] == "Curriculum Vitae"


def test_docx_visual_identity_uses_source_hash_or_pinned_facts_and_rejects_bad_hash():
    template = {"source_sha256": "a" * 64, "facts": [{"id": "f1", "text": "QA"}]}
    assert template_visual_reference(template) == {"sha256": "a" * 64}
    template.pop("source_sha256")
    assert template_visual_reference(template) == {
        "sha256": content_hash({"facts": template["facts"], "style": {}})
    }
    template["source_sha256"] = "not-a-sha"
    with pytest.raises(PolicyViolation, match="visual identity"):
        template_visual_reference(template)


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


def visual_review(document):
    return {
        "verdict": "pass",
        "issues": [],
        "template_sha256": "a" * 64,
        "rendered_sha256": "b" * 64,
        "draft_hash": content_hash(document),
    }


def test_visual_review_binds_both_files_and_document():
    document = draft()
    result = visual_review(document)
    assert (
        validate_visual_review(result, document, {"sha256": "a" * 64}, {"sha256": "b" * 64})
        == result
    )
    revised = result | {"verdict": "revise", "issues": ["Increase title spacing"]}
    assert bind_visual_review(
        revised, document, {"sha256": "a" * 64}, {"sha256": "b" * 64}
    ) == revised


@pytest.mark.parametrize(
    "change",
    [
        {"approved": True},
        {"verdict": "needs_input"},
        {"template_sha256": "bad"},
        {"template_sha256": "0" * 64},
        {"rendered_sha256": "0" * 64},
        {"draft_hash": "0" * 64},
        {"verdict": "revise", "issues": ["Clipped content"]},
        {"verdict": "pass", "issues": ["Missing section"]},
        {"verdict": "revise", "issues": []},
    ],
)
def test_visual_review_fails_closed(change):
    document = draft()
    with pytest.raises(PolicyViolation):
        validate_visual_review(
            visual_review(document) | change, document, {"sha256": "a" * 64}, {"sha256": "b" * 64}
        )


def test_paragraph_roles_are_validated_and_legacy_hashes_preserved():
    document = draft()
    paragraph = document["sections"][0]["paragraphs"][0]
    paragraph["kind"] = "body"
    assert content_hash(validate_draft(document, context())) == content_hash(draft())
    paragraph["kind"] = "entry"
    assert validate_draft(document, context())["sections"][0]["paragraphs"][0]["kind"] == "entry"
    paragraph["kind"] = "script"
    with pytest.raises(PolicyViolation):
        validate_draft(document, context())


def test_cv_title_cannot_be_repeated_as_a_body_paragraph():
    document = draft()
    document["sections"][0]["paragraphs"][0]["text"] = document["title"]
    with pytest.raises(PolicyViolation, match="must not be repeated"):
        validate_draft(document, context())
    assert validate_draft(document, context() | {"kind": "cover_letter"})


def test_long_experience_section_keeps_all_entries_with_bounded_size():
    document = draft()
    paragraph = document["sections"][0]["paragraphs"][0]
    document["sections"][0]["paragraphs"] = [copy.deepcopy(paragraph) for _ in range(200)]
    assert len(validate_draft(document, context())["sections"][0]["paragraphs"]) == 200
    document["sections"][0]["paragraphs"].append(copy.deepcopy(paragraph))
    with pytest.raises(PolicyViolation, match="structure"):
        validate_draft(document, context())
