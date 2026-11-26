import re

CLAUSE_BOUNDARY = re.compile(r"(?<!\bst)\.\s+|[!?;]\s+|\n+")
RUSSIAN_COMPANY = (
    r"(?:(?:russian|russia-based|russian-founded|moscow-based) "
    r"(?:(?:it|tech|technology|software|fintech|product) ){0,2}(?:company|business|startup)|"
    r"(?:российская|русская) (?:(?:ит|it|технологическая|продуктовая)[ -]){0,2}компания|"
    r"компания (?:родом )?из россии)\b"
)
COMPANY_DESCRIPTION = re.compile(
    r"^(?:(?:about us|о компании):\s*|(?:we are|we're|(?:our|the) company is|is) (?:an? )?|"
    r"мы\s*(?:[—–-]\s*)?|(?:(?:наша )?компания\s*)?[—–-]\s*)"
    rf"{RUSSIAN_COMPANY}"
    rf"|^{RUSSIAN_COMPANY}(?:$| (?:is hiring|ищет|нанимает)\b)"
)
RUSSIAN_PLACE = r"russia|moscow|saint petersburg|st\.? petersburg|россии|рф|москве|санкт-петербурге"
COMPANY_BASE = re.compile(
    r"^(?:(?:we (?:are|were)|(?:(?:our|the) )?company(?: (?:is|was))?|is|was) "
    r"(?:founded|headquartered|based) in |"
    r"(?:our |company )?(?:headquarters|head office|hq)(?: (?:are|is))?(?: in\s+|:\s*)|"
    r"(?:(?:наша )?компания )?основана в |"
    r"(?:наш[а]? )?(?:штаб[- ]квартира|головной офис)(?: находится)?(?: в\s+|:\s*))"
    rf"(?:{RUSSIAN_PLACE})\b"
    rf"|^(?:founded|headquartered) in (?:{RUSSIAN_PLACE})$"
)


def is_russian_company(description: str, company: str = "") -> bool:
    company_name = company.casefold().strip()
    text = description[:20000].casefold().replace("’", "'")
    for clause in CLAUSE_BOUNDARY.split(text):
        clause = clause.strip(" \t•*-#.!?;")
        if company_name and clause.startswith(f"{company_name} "):
            clause = clause[len(company_name) :].lstrip()
        if COMPANY_DESCRIPTION.search(clause) or COMPANY_BASE.search(clause):
            return True
    return False
