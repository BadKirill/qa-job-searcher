from __future__ import annotations

import re


def extract_details(description: str, title: str = "") -> dict[str, list[str]]:
    details: dict[str, list[str]] = {
        "languages": [],
        "programming_languages": [],
        "qa_types": [],
        "responsibilities": [],
        "requirements": [],
    }
    headings = {
        "responsibilities": r"^(?:(?:(?:key|main|core|primary)\s+)?responsibilities|"
        r"what you(?:'ll| will) do|your (?:tasks|role)|"
        r"обязанности|задачи|что (?:делать|предстоит делать))\s*[:：]?\s*$",
        "requirements": r"^(?:(?:(?:key|technical|essential)\s+)?requirements|"
        r"(?:(?:required|preferred|minimum|basic|essential|desired)\s*)?qualifications|"
        r"nice[ -]to[ -]have|хорошо,? если есть|будет плюсом|"
        r"what (?:we(?:'re| are) looking for|you bring)|"
        r"требования|что (?:нужно|ожидаем)|навыки)\s*[:：]?\s*$",
    }
    current = ""
    for line in description.splitlines():
        line = line.strip(" \t•*-#")
        if not line:
            current = ""
            continue
        heading = next(
            (key for key, pattern in headings.items() if re.match(pattern, line, re.I)), ""
        )
        if heading:
            current = heading
            continue
        if re.match(
            r"^(?:(?:benefits|culture(?:\s*(?:&|and)\s*benefits)?|"
            r"hiring process|recruitment process|about (?:us|(?:the )?company)|"
            r"we offer|what we offer|"
            r"compensation|salary|perks|(?:how to|ready to) apply|equal[ -]opportunity|"
            r"условия|культура и преимущества|этапы (?:найма|отбора)|"
            r"о компании|мы предлагаем)\b|apply(?: now| today)?\s*[:：]?\s*$)",
            line,
            re.I,
        ):
            current = ""
        elif current and len(details[current]) < 12:
            details[current].append(line[:1200])
        if re.search(
            r"\b(?:English|German|French|Spanish|Serbian|Russian)\b|"
            r"английск|немецк|французск|испанск|сербск|русск",
            line,
            re.I,
        ):
            details["languages"].append(line[:350])
    text = f"{title}\n{description}"
    for language in (
        "Python",
        "TypeScript",
        "JavaScript",
        "Java",
        "C#",
        "C++",
        "Kotlin",
        "Go",
        "SQL",
    ):
        if re.search(rf"(?<!\w){re.escape(language)}(?!\w)", text):
            details["programming_languages"].append(language)
    for label, pattern in {
        "Manual": r"\bmanual (?:qa|test(?:ing|er)?)\b|ручн\w* тестирован",
        "Auto": r"\b(?:test automation|automation (?:qa|test)|automated test|sdet)\b|"
        r"автоматизац\w* тест",
        "Fullstack": r"\bfull[ -]?stack (?:qa|test)\b",
        "AI": r"\b(?:ai|llm|genai|machine learning) (?:test(?:ing|er)?|quality|eval\w*)\b|"
        r"тестирован\w* (?:ии|ai|llm)",
    }.items():
        if re.search(pattern, text, re.I):
            details["qa_types"].append(label)
    return {key: list(dict.fromkeys(values)) for key, values in details.items()}
