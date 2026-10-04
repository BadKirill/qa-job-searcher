import pytest

from qa_job_bot.job_details import extract_details


def test_empty_description_omits_every_optional_field() -> None:
    assert extract_details("") == {
        "languages": [],
        "programming_languages": [],
        "qa_types": [],
        "responsibilities": [],
        "requirements": [],
    }


def test_extracts_explicit_sections_languages_and_types_without_changing_evidence() -> None:
    text = (
        "Responsibilities:\n• Manual testing\n- Test automation with Python\n"
        "Requirements\nEnglish B2\nTypeScript and SQL\nEnglish B2\n"
        "Benefits\nFree lunches\n\nAI testing and fullstack QA\n"
    )
    result = extract_details(text)
    assert result["responsibilities"] == ["Manual testing", "Test automation with Python"]
    assert result["requirements"] == ["English B2", "TypeScript and SQL"]
    assert result["languages"] == ["English B2"]
    assert result["programming_languages"] == ["Python", "TypeScript", "SQL"]
    assert result["qa_types"] == ["Manual", "Auto", "Fullstack", "AI"]


@pytest.mark.parametrize(
    "heading",
    [
        "What you'll do:",
        "Your tasks",
        "Что предстоит делать",
        "ОБЯЗАННОСТИ",
        "Задачи",
        "Key Responsibilities",
        "Core responsibilities:",
        "Primary Responsibilities",
    ],
)
def test_responsibility_heading_variants(heading: str) -> None:
    assert extract_details(f"{heading}\nTest APIs")["responsibilities"] == ["Test APIs"]


@pytest.mark.parametrize(
    "heading",
    [
        "Qualifications",
        "What we're looking for",
        "What you bring",
        "Требования",
        "Навыки",
        "Required Qualifications",
        "PreferredQualifications",
        "Minimum qualifications:",
        "Technical requirements",
    ],
)
def test_requirement_heading_variants(heading: str) -> None:
    assert extract_details(f"{heading}\nAPI experience")["requirements"] == ["API experience"]


def test_blank_line_and_other_section_terminate_collection() -> None:
    result = extract_details(
        "Requirements\nAPI\n\nUnrelated paragraph\nResponsibilities\nTest\n"
        "О компании\nCompany overview"
    )
    assert result["requirements"] == ["API"]
    assert result["responsibilities"] == ["Test"]


def test_limits_section_lines_and_lengths_without_repeating_values() -> None:
    lines = [str(index) + "x" * 1300 for index in range(13)]
    result = extract_details("Requirements\n" + "\n".join(lines))
    assert len(result["requirements"]) == 12
    assert all(len(line) == 1200 for line in result["requirements"])
    result = extract_details("English " + "x" * 500)
    assert len(result["languages"][0]) == 350


def test_stack_token_boundaries_and_no_inferred_qa_types() -> None:
    result = extract_details(
        "AI company creating assistants. Experience with JavaScript and C++.", "QA Engineer"
    )
    assert result["programming_languages"] == ["JavaScript", "C++"]
    assert result["qa_types"] == []
    assert result["languages"] == []
    assert extract_details("ручное тестирование и автоматизация тестов")["qa_types"] == [
        "Manual",
        "Auto",
    ]
    assert extract_details("", "SDET")["qa_types"] == ["Auto"]


@pytest.mark.parametrize(
    "heading",
    [
        "What we offer:",
        "Compensation",
        "Salary:",
        "Perks",
        "About the company",
        "About company",
        "How to apply",
        "Apply now",
        "Apply:",
        "Ready to apply?",
        "Equal Opportunity Employer",
        "Equal-opportunity statement",
    ],
)
def test_offer_salary_company_and_application_sections_are_not_requirements(heading: str) -> None:
    result = extract_details(
        f"Key Responsibilities\nTest the product\n"
        f"Required Qualifications\nAPI experience\n{heading}\n"
        "Paid vacation and compensation details"
    )
    assert result["responsibilities"] == ["Test the product"]
    assert result["requirements"] == ["API experience"]


def test_explicit_preferred_qualifications_continue_requirement_collection() -> None:
    result = extract_details(
        "Key Responsibilities\nApply modern testing techniques\n"
        "What we offer:\nFlexible schedule\nRequired Qualifications\n"
        "API experience\nPreferred Qualifications\nPython experience\n"
        "Compensation\n5000 EUR per month"
    )
    assert result["responsibilities"] == ["Apply modern testing techniques"]
    assert result["requirements"] == ["API experience", "Python experience"]
