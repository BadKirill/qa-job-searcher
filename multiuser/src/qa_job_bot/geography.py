import json
import re
from dataclasses import asdict, dataclass
from difflib import get_close_matches
from importlib.resources import files


@dataclass(frozen=True)
class Place:
    id: str
    kind: str
    name: str
    label: str
    aliases: tuple[str, ...] = ()
    country: str | None = None


COUNTRY_NAMES = json.loads(files("qa_job_bot").joinpath("country_names.json").read_text("utf-8"))
ALIASES = {
    "GB": ("UK", "Great Britain", "Великобритания"),
    "GE": ("Sakartvelo", "Republic of Georgia", "Грузия страна", "Грузии"),
    "RS": ("Сербии", "Сербию"),
    "DE": ("Германии", "Германию"),
    "TR": ("Turkey", "Турция"),
    "US": ("US", "USA", "U.S.", "United States of America"),
    "CZ": ("Czech Republic",),
    "RU": ("Russian Federation", "РФ"),
    "KR": ("Republic of Korea",),
}
COUNTRIES = [
    Place(f"country:{code}", "country", en, ru, ALIASES.get(code, ()))
    for code, (en, ru) in COUNTRY_NAMES.items()
]
CITIES = [
    Place(f"city:{key}", "city", en, ru, aliases, f"country:{country}")
    for key, en, ru, country, aliases in (
        ("ge-tbilisi", "Tbilisi", "Тбилиси", "GE", ("თბილისი",)),
        ("rs-belgrade", "Belgrade", "Белград", "RS", ("Beograd",)),
        ("rs-novi-sad", "Novi Sad", "Нови-Сад", "RS", ("Нови Сад",)),
        ("de-berlin", "Berlin", "Берлин", "DE", ()),
        ("de-munich", "Munich", "Мюнхен", "DE", ("München",)),
        ("de-hamburg", "Hamburg", "Гамбург", "DE", ()),
        ("us-berlin-nh", "Berlin", "Берлин (Нью-Гэмпшир)", "US", ()),
        ("us-new-york", "New York City", "Нью-Йорк", "US", ("New York", "NYC")),
        ("us-san-francisco", "San Francisco", "Сан-Франциско", "US", ()),
        ("us-atlanta", "Atlanta", "Атланта", "US", ()),
        ("gb-london", "London", "Лондон", "GB", ()),
        ("fr-paris", "Paris", "Париж", "FR", ()),
        ("nl-amsterdam", "Amsterdam", "Амстердам", "NL", ()),
        ("es-barcelona", "Barcelona", "Барселона", "ES", ()),
        ("es-madrid", "Madrid", "Мадрид", "ES", ()),
        ("pt-lisbon", "Lisbon", "Лиссабон", "PT", ("Lisboa",)),
        ("pl-warsaw", "Warsaw", "Варшава", "PL", ("Warszawa",)),
        ("cz-prague", "Prague", "Прага", "CZ", ("Praha",)),
        ("tr-istanbul", "Istanbul", "Стамбул", "TR", ("İstanbul",)),
        ("ae-dubai", "Dubai", "Дубай", "AE", ()),
        ("am-yerevan", "Yerevan", "Ереван", "AM", ()),
        ("kz-almaty", "Almaty", "Алматы", "KZ", ()),
        ("ru-moscow", "Moscow", "Москва", "RU", ()),
        ("cy-limassol", "Limassol", "Лимасол", "CY", ()),
    )
]
REGIONS = [
    Place(
        "region:world",
        "region",
        "Worldwide",
        "Весь мир",
        (
            "anywhere",
            "global",
            "по всему миру",
            "work from anywhere",
        ),
    ),
    Place("region:eu", "region", "European Union", "Европейский союз", ("EU", "ЕС")),
    Place("region:eea", "region", "European Economic Area", "ЕЭЗ", ("EEA",)),
    Place("region:europe", "region", "Europe", "Европа"),
    Place("region:emea", "region", "EMEA", "Европа, Ближний Восток и Африка", ("емеа",)),
    Place("region:mena", "region", "MENA", "Ближний Восток и Северная Африка", ("мена", "ме")),
    Place("region:latam", "region", "Latin America", "Латинская Америка", ("LATAM",)),
    Place("region:apac", "region", "Asia Pacific", "Азиатско-Тихоокеанский регион", ("APAC",)),
    Place("region:cis", "region", "CIS", "СНГ"),
]
PLACES = {
    place.id: place
    for place in [
        *COUNTRIES,
        *CITIES,
        *REGIONS,
        Place(
            "state:us-ga",
            "state",
            "Georgia",
            "Джорджия (штат)",
            (
                "GA",
                "Georgia USA",
                "Джорджия",
            ),
            "country:US",
        ),
    ]
}
EU_COUNTRIES = frozenset(
    f"country:{code}"
    for code in [
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    ]
)
REGION_MEMBERS = {
    "region:eu": EU_COUNTRIES,
    "region:eea": EU_COUNTRIES | {"country:IS", "country:LI", "country:NO"},
}


def region_codes(codes):
    return frozenset(codes.split())


PREFERENCE_REGIONS = {
    "region:europe": region_codes(
        "AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GE GI GR HR HU IE IM IS "
        "IT LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM UA VA"
    ),
    "region:mena": region_codes("AE BH DZ EG IL IQ IR JO KW LB LY MA OM PS QA SA SD SY TN YE"),
    "region:latam": region_codes("AR BO BR CL CO CR CU DO EC GT HN MX NI PA PE PR PY SV UY VE"),
    "region:apac": region_codes(
        "AU BD BN BT CN FJ HK ID IN JP KH KI KP KR LA LK MM MN MO MY NP NZ PH PK PG SG TH TL TW VN"
    ),
    "region:cis": region_codes("AM AZ BY KZ KG MD RU TJ TM UZ"),
}
PREFERENCE_REGIONS["region:emea"] = (
    PREFERENCE_REGIONS["region:europe"]
    | PREFERENCE_REGIONS["region:mena"]
    | region_codes(
        "AO BF BI BJ BW CD CF CG CI CM DJ ER ET GA GH GM GN GQ GW KE KM LR LS MG ML MR MU MW "
        "MZ NA NE NG RW SC SL SN SO SS ST SZ TD TG TR TZ UG ZA ZM ZW"
    )
)


def preference_covers(scope, location):
    if scope in {"region:europe", "region:emea"} and location in REGION_MEMBERS:
        return True
    country = country_of(location)
    return covers(scope, location) or (
        country is not None
        and country.removeprefix("country:") in PREFERENCE_REGIONS.get(scope, ())
    )


def normalize(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def aliases(place):
    return {normalize(value) for value in (place.name, place.label, *place.aliases)}


def mentions(place):
    return {
        key
        for key in aliases(place)
        if len(key) > 2 or key in {"us", "uk", "eu", "ес", "рф", "снг"}
    }


PLACE_ALIASES = {place_id: aliases(place) for place_id, place in PLACES.items()}
MENTION_KEYS = sorted(
    {key for place in PLACES.values() for key in mentions(place)}, key=lambda key: (-len(key), key)
)
MENTION_PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, MENTION_KEYS)) + r")(?!\w)")
MENTION_IDS = {
    key: {place.id for place in PLACES.values() if key in PLACE_ALIASES[place.id]}
    for key in MENTION_KEYS
}


def place_label(place_id):
    place = PLACES[place_id]
    if place.kind == "region" and place.name in {"MENA", "EMEA", "CIS"}:
        return f"{place.name} · {place.label}"
    if place.country:
        return f"{place.label}, {PLACES[place.country].label}"
    return place.label


def find_places(query="", kinds=()):
    query = normalize(query)
    eligible = [place for place in PLACES.values() if not kinds or place.kind in kinds]
    matched = [
        place
        for place in eligible
        if any(value.startswith(query) for value in PLACE_ALIASES[place.id])
    ]
    corrected = False
    if query and not matched and len(query) >= 3:
        similar = set(
            get_close_matches(
                query, {key for place in eligible for key in PLACE_ALIASES[place.id]}, 4, 0.72
            )
        )
        matched = [place for place in eligible if similar & PLACE_ALIASES[place.id]]
        corrected = bool(matched)
    matched.sort(key=lambda place: (query not in PLACE_ALIASES[place.id], place_label(place.id)))
    return {
        "items": [{**asdict(place), "label": place_label(place.id)} for place in matched[:30]],
        "corrected": corrected,
    }


def exact_places(value):
    if value in PLACES:
        return {value}
    value = normalize(value)
    return {key for key, names in PLACE_ALIASES.items() if value in names}


def identified_places(values):
    result = set()
    for value in values:
        matches = exact_places(value)
        if len(matches) == 1:
            result.update(matches)
        elif matches == {"country:GE", "state:us-ga"} and value.casefold() != "georgia":
            result.add("country:GE")
    return result


def mentioned_places(text):
    mentions = [MENTION_IDS[match.group()] for match in MENTION_PATTERN.finditer(normalize(text))]
    explicit = set().union(*(items for items in mentions if len(items) == 1))
    for items in mentions:
        if (
            items == {"country:GE", "state:us-ga"}
            and any(key.startswith("country:") and key != "country:US" for key in explicit)
            and "country:US" not in explicit
        ):
            explicit.add("country:GE")
        if len(items) != 1:
            explicit.update(item for item in items if PLACES[item].country in explicit)
    return explicit


def country_of(place_id):
    place = PLACES[place_id]
    return place.country or (place_id if place.kind == "country" else None)


def covers(scope, candidate):
    country = country_of(candidate)
    return scope in (candidate, country, "region:world") or country in REGION_MEMBERS.get(scope, ())
