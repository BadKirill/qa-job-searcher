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

INTRO = re.compile(
    r"^(?:we (?:are(?! (?:looking|hiring|seeking|recruiting))|build|develop|provide|help)|"
    r"мы (?:созда[её]м|разрабатываем|помогаем)).+",
    re.I,
)
ROLE = re.compile(r"^(?:(?:in this role[,]? )?you(?: will|'ll)|вам предстоит|вы будете)\b.+", re.I)
INDUSTRY = re.compile(
    r"\b(?:fintech|финтех|healthcare|edtech|e-commerce|insurtech|cybersecurity|"
    r"телеком|кибербезопасность)\b(?=\s+(?:company|platform|business|компания|платформа))|"
    r"\b(?:in the|в сфере)\s+([a-zа-яё][a-zа-яё -]{1,40}?)\s+(?:industry|sector)\b",
    re.I,
)
SIZE = re.compile(
    r"\b(?:\d{1,6}(?:\s*[–—-]\s*\d{1,6})?\+?\s+(?:employees|сотрудников|сотрудника)|"
    r"team of \d{1,6}(?:\s*[–—-]\s*\d{1,6})?\+?(?:\s+(?:people|employees))?)\b",
    re.I,
)
FOUNDED = re.compile(
    r"\b(?:founded in|основан[аоы]? в)\s+((?:18|19|20)\d{2})\b", re.I,
)
COMPANY_HEADING = re.compile(r"^(?:about (?:us|the company)|о компании)\s*[:：]?$", re.I)
ROLE_HEADING = re.compile(r"^(?:about the role|role focus|о роли|фокус роли)\s*[:：]?$", re.I)


def posting_summary(description, details, url):
    lines = [line for value in description.splitlines() if (line := value.strip(" \t*•-#"))]
    items = []
    extracted = set()

    def add(field, value, evidence):
        extracted.add(field)
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

    company_context = [
        line
        for index, line in enumerate(lines)
        if INTRO.match(line) or (index and COMPANY_HEADING.fullmatch(lines[index - 1]))
    ]
    for field, pattern in (
        ("company_focus", INTRO),
        ("industry", INDUSTRY),
        ("company_size", SIZE),
        ("founded", FOUNDED),
    ):
        if field in extracted:
            continue
        for line in company_context:
            match = pattern.search(line)
            if field == "company_focus" or match:
                value = (match[1] if match.lastindex else match[0]) if match else line
                add(field, value, line)
                break
    if "role_focus" not in extracted:
        for index, line in enumerate(lines):
            if ROLE.match(line) or (index and ROLE_HEADING.fullmatch(lines[index - 1])):
                add("role_focus", line, line)
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
