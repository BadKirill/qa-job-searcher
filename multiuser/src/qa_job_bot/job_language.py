import re

from langdetect import DetectorFactory, LangDetectException, detect_langs

from qa_job_bot.company_origin import is_russian_company

DetectorFactory.seed = 0

ENGLISH = r"english|английск\w*"
RUSSIAN = r"russian|русск\w*"
OTHER_LANGUAGES = (
    r"arabic|chinese|czech|dutch|french|german|hindi|italian|japanese|korean|"
    r"polish|portuguese|serbian|spanish|turkish|ukrainian|"
    r"арабск\w*|голландск\w*|испанск\w*|итальянск\w*|китайск\w*|корейск\w*|"
    r"немецк\w*|нидерландск\w*|польск\w*|португальск\w*|сербск\w*|турецк\w*|"
    r"украинск\w*|французск\w*|хинди|чешск\w*|японск\w*"
)
LANGUAGE = rf"\b(?:{ENGLISH}|{RUSSIAN}|{OTHER_LANGUAGES})\b"
SEPARATOR = r"(?:\s*(?:,\s*(?:(?:and|or|и|или)\s+)?|[/&+])\s*|\s+(?:and|or|и|или)\s+)"
LANGUAGE_LIST = rf"{LANGUAGE}(?:{SEPARATOR}{LANGUAGE})*"
ENGLISH_NAME = re.compile(rf"\b(?:{ENGLISH})\b")
RUSSIAN_NAME = re.compile(rf"\b(?:{RUSSIAN})\b")
WORK_LANGUAGE = re.compile(
    r"\b(?:(?:working|work|company|team|internal|communication) languages?"
    r"(?: (?:of|in) (?:the |our )?(?:company|team|workplace))?(?: (?:is|are))?|"
    r"(?:(?:the|our) (?:team|company)|we) (?:speaks?|communicates? in|works? in)|"
    r"(?:team|internal) communication (?:is|takes place) in|"
    r"рабоч(?:ий|ие) язык(?:и)?(?: (?:в |у )?(?:команд[еы]|компани[ии]))?|"
    r"язык(?:и)? (?:(?:работы|общения|коммуникации) )?(?:в |внутри )?(?:команд[еы]|компани[ии])|"
    r"язык(?:и)? (?:работы|общения|коммуникации)|"
    r"(?:(?:команда|компания|мы) (?:говор\w*|обща\w*|работа\w*)|"
    r"(?:в команде |в компании )?(?:говорим|общаемся|работаем)) на)"
    rf"[ \t]*(?:[:—–-][ \t]*)?({LANGUAGE_LIST})"
    rf"|({LANGUAGE_LIST})\s+(?:is|are) (?:our|the) (?:primary )?"
    r"(?:working|work|company|team) languages?\b"
)
SPEAKING_TEAM = re.compile(
    r"(?<!\bnot a )(?<!\bnot an )(?<!\bnot )(?<!\bне )(?<!\bnon-)"
    rf"\b(?:({LANGUAGE})[- ]speaking (?:team|company)\b|"
    r"((?:русскоязычн|англоязычн)\w*) (?:команд|компан)\w*)"
)


def working_languages(description: str, company: str = "") -> set[str]:
    text = description[:20000].strip()
    normalized = text.casefold()
    declarations = WORK_LANGUAGE.findall(normalized) or SPEAKING_TEAM.findall(normalized)
    if declarations:
        evidence = "\n".join(first or second for first, second in declarations)
        return {
            code
            for code, name, adjective in (
                ("en", ENGLISH_NAME, "англоязычн"),
                ("ru", RUSSIAN_NAME, "русскоязычн"),
            )
            if name.search(evidence) or adjective in evidence
        }
    if is_russian_company(text, company):
        return {"ru"}
    if len(text) < 40:
        return set()
    try:
        guess = detect_langs(text)[0]
    except LangDetectException:
        return set()
    return {guess.lang} if guess.lang in {"en", "ru"} and guess.prob >= 0.75 else set()


REQUIRED_HEADING = re.compile(
    r"^(?:requirements?|qualifications?|languages? required|требования|обязательные языки)"
    r"\s*(?::\s*(.*))?$",
    re.I,
)
REQUIRED_WORDS = re.compile(
    r"\brequir\w*\b|\bmust\b|\bfluency\b|\bfluent\b|\bproficiency\b|"
    r"обязатель|требу|владение",
    re.I,
)
OPTIONAL_WORDS = re.compile(
    r"nice.to.have|\bpreferred\b|\boptional\b|\bplus\b|плюсом|желательно",
    re.I,
)
LANGUAGE_ALTERNATIVE = re.compile(r"\s+(?:or|или)\s+|\s*/\s*", re.I)


def language_evidence(description):
    lines = description[:20000].splitlines()
    evidence = []
    in_requirements = False
    for original in lines:
        line = original.strip(" \t•*-#")
        if not line:
            in_requirements = False
            continue
        heading = REQUIRED_HEADING.match(line)
        if heading:
            in_requirements = True
            line = heading[1] or ""
        elif line.endswith(":") and not REQUIRED_WORDS.search(line):
            in_requirements = False
        normalized = line.casefold()
        declarations = WORK_LANGUAGE.findall(normalized) or SPEAKING_TEAM.findall(normalized)
        if declarations:
            value = " ".join(first or second for first, second in declarations)
            languages = [
                code
                for code, pattern, adjective in (
                    ("en", ENGLISH_NAME, "англоязычн"),
                    ("ru", RUSSIAN_NAME, "русскоязычн"),
                )
                if pattern.search(value) or adjective in value
            ]
            if languages:
                evidence.append(
                    {"kind": "team", "languages": languages, "evidence": original[:350]}
                )
        if (in_requirements or REQUIRED_WORDS.search(line)) and not OPTIONAL_WORDS.search(line):
            alternatives = [
                [
                    code
                    for code, pattern in (("en", ENGLISH_NAME), ("ru", RUSSIAN_NAME))
                    if pattern.search(part)
                ]
                for part in LANGUAGE_ALTERNATIVE.split(normalized)
            ]
            alternatives = [item for item in alternatives if item]
            if alternatives and (not declarations or REQUIRED_WORDS.search(line)):
                evidence.append(
                    {
                        "kind": "required",
                        "languages": sorted(set().union(*map(set, alternatives))),
                        "alternatives": alternatives,
                        "evidence": original[:350],
                    }
                )
    return evidence


def matches_working_languages(description, company, selected):
    explicit = [item for item in language_evidence(description) if item["kind"] == "required"]
    if explicit:
        return all(
            any(set(alternative) <= set(selected) for alternative in item["alternatives"])
            for item in explicit
        )
    return not working_languages(description, company).isdisjoint(selected)


def language_display(description, company):
    evidence = language_evidence(description)
    if evidence:
        return evidence
    if is_russian_company(description, company):
        return [{"kind": "company_inferred", "languages": ["ru"], "evidence": ""}]
    inferred = sorted(working_languages(description, ""))
    return [{"kind": "inferred", "languages": inferred, "evidence": ""}] if inferred else []
