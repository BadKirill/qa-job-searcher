import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

SOURCE_HOSTS = {
    "remotive.com": "remotive",
    "himalayas.app": "himalayas",
    "weworkremotely.com": "weworkremotely",
    "remote.com": "remote_com",
    "hirify.me": "hirify",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "t.me": "telegram",
    "telegram.me": "telegram",
}
AGGREGATOR_NAMES = "Remotive, Himalayas, We Work Remotely, Remote.com, Hirify"
SLUG = re.compile(r"[A-Za-z0-9_-]{1,80}")
POSTING_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class PostingLink:
    kind: str
    board: str
    posting: str
    region: str
    url: str


def https_parts(value):
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("Укажите корректную публичную ссылку HTTPS.")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Укажите корректную публичную ссылку HTTPS.") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or port not in {None, 443}
    ):
        raise ValueError("Укажите публичную ссылку HTTPS без данных входа.")
    path = [unquote(part) for part in url.path.strip("/").split("/") if part]
    if any(part in {".", ".."} or "/" in part or "\\" in part for part in path):
        raise ValueError("Недопустимый путь ссылки.")
    return url, path


def source_link(value):
    if value.startswith("@"):
        return "telegram", {"handle": value[1:]}
    url, path = https_parts(value)
    host = url.hostname.removeprefix("www.")
    kind = SOURCE_HOSTS.get(host)
    if not kind:
        raise ValueError(
            f"Источник не поддерживается. Доступны {AGGREGATOR_NAMES}, Greenhouse, "
            "Lever, Ashby и публичный Telegram."
        )
    if kind in {"greenhouse", "lever", "ashby"} and path and SLUG.fullmatch(path[0]):
        return kind, {"token": path[0], "region": "eu" if host == "jobs.eu.lever.co" else "global"}
    if kind == "telegram":
        parts = path[1:] if path[:1] == ["s"] else path
        if (len(parts) == 1 or (len(parts) == 2 and parts[1].isdecimal())) and re.fullmatch(
            r"[A-Za-z0-9_]{5,32}", parts[0]
        ):
            return kind, {"handle": parts[0]}
        raise ValueError("Укажите ссылку на публичный канал Telegram.")
    if kind not in {"greenhouse", "lever", "ashby"}:
        return kind, {}
    raise ValueError("Укажите ссылку на карьерную доску компании.")


def posting_link(value):
    url, path = https_parts(value)
    host = url.hostname
    kind = SOURCE_HOSTS.get(host)
    if kind not in {"greenhouse", "lever", "ashby"} or not path or not SLUG.fullmatch(path[0]):
        return None
    if kind == "greenhouse":
        valid = len(path) == 3 and path[1] == "jobs" and bool(re.fullmatch(r"[0-9]{1,20}", path[2]))
        if valid:
            return PostingLink(
                kind,
                path[0],
                path[2],
                "global",
                f"https://job-boards.greenhouse.io/{path[0]}/jobs/{path[2]}",
            )
    elif (
        len(path) in (2, 3)
        and (len(path) == 2 or path[2] == "apply")
        and POSTING_ID.fullmatch(path[1])
    ):
        region = "eu" if host == "jobs.eu.lever.co" else "global"
        return PostingLink(
            kind, path[0], path[1].lower(), region, f"https://{host}/{path[0]}/{path[1].lower()}"
        )
    return None


def csv_export_url(value):
    url, path = https_parts(value)
    if url.hostname != "docs.google.com" or path[:2] != ["spreadsheets", "d"]:
        raise ValueError("Укажите публичную ссылку Google Sheets с доступным CSV-экспортом.")
    published = len(path) == 5 and path[2] == "e" and path[4] == "pub"
    if not published and (len(path) not in (3, 4) or path[2] == "e"):
        raise ValueError("Не удалось определить таблицу Google Sheets.")
    key = path[3] if published else path[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", key):
        raise ValueError("Некорректный идентификатор таблицы.")
    query = parse_qs(url.query)
    gid = (query.get("gid") or parse_qs(url.fragment).get("gid") or [""])[0]
    params = {"output" if published else "format": "csv"}
    if gid.isascii() and gid.isdecimal() and len(gid) <= 12:
        params["gid"] = gid
    root = f"https://docs.google.com/spreadsheets/d/{'e/' if published else ''}{key}"
    return f"{root}/{'pub' if published else 'export'}?{urlencode(params)}"
