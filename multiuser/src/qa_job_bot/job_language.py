import re

from langdetect import DetectorFactory, LangDetectException, detect_langs

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


def working_languages(description: str) -> set[str]:
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
    if len(text) < 40:
        return set()
    try:
        guess = detect_langs(text)[0]
    except LangDetectException:
        return set()
    return {guess.lang} if guess.lang in {"en", "ru"} and guess.prob >= 0.75 else set()
