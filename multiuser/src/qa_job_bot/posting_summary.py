import re

FIELDS = (
    ("company_focus", r"^(?:about (?:us|the company)|о компании)\s*[:：]\s*(.+)$"),
    ("industry", r"^(?:industry|отрасль)\s*[:：]\s*(.+)$"),
    ("company_size", r"^(?:company size|employees|размер компании|сотрудников)\s*[:：]\s*(.+)$"),
    ("founded", r"^(?:founded|год основания|основана)\s*[:：]\s*((?:18|19|20)\d{2})\b"),
    ("role_focus", r"^(?:role focus|about the role|о роли|фокус роли)\s*[:：]\s*(.+)$"),
    (
        "experience",
        r"\b(?:at least |minimum of |от |не менее )?(\d{1,2}\+?)\s*(?:years?|лет|года?)"
        r"\s*(?:of |опыта)?",
    ),
    ("benefits", r"^(?:benefits|what we offer|мы предлагаем|условия)\s*[:：]\s*(.+)$"),
)


def posting_summary(description, details, url):
    lines = [line.strip(" \t*•-") for line in description.splitlines()]
    items = []

    def add(field, value, evidence):
        items.append(
            {
                "field": field,
                "value": value[:260],
                "evidence": evidence[:500],
                "url": url,
            }
        )

    for field, template in FIELDS:
        pattern = re.compile(template, re.I)
        candidates = (
            details["requirements"] if field == "experience" and details["requirements"] else lines
        )
        for line in candidates:
            if field == "experience" and not re.search(
                r"experience|working|developing|опыт|стаж", line, re.I
            ):
                continue
            if match := pattern.search(line):
                add(
                    field,
                    f"От {match[1].removesuffix('+')} лет" if field == "experience" else match[1],
                    line,
                )
                break
    for field in ("responsibilities", "requirements"):
        for line in details[field][:2]:
            add(field, line, line)
    names = details["programming_languages"]
    if names:
        value = ", ".join(names)
        evidence = next(
            (line for line in lines if any(name.casefold() in line.casefold() for name in names)),
            "",
        )
        if evidence:
            add("stack", value, evidence)
    return items
