import csv
import io
import re
from string import ascii_lowercase, digits
from urllib.parse import urljoin, urlsplit

import httpx

from qa_job_bot.search_models import SourceSpec, clean_text, source_identity
from qa_job_bot.source_http import SourceClient, SourceNetworkError
from qa_job_bot.source_links import csv_export_url, source_link
from qa_job_bot.web_store import StoreError

MAX_CSV_BYTES = 256 * 1024
MAX_ROWS = 100
SHEETS_DOWNLOAD_LABEL_CHARS = frozenset(ascii_lowercase + digits + "-")


def link_spec(value, name="", tags=()):
    kind, config = source_link(value)
    if kind in {"greenhouse", "lever", "ashby"} and name:
        config["name"] = name
    if config and tags:
        config["tags"] = list(tags)
    spec = SourceSpec(kind=kind, config=config)
    spec.normalized()
    return spec


def csv_rows(raw, identities):
    if not raw or len(raw) > MAX_CSV_BYTES:
        raise StoreError("Выберите непустой CSV размером до 256 КБ.", 422)
    try:
        stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
        first = stream.readline()
        stream.seek(0)
        reader = csv.reader(
            stream, delimiter=";" if first.count(";") > first.count(",") else ",", strict=True
        )
        headers = next(reader, [])
        columns = {name.strip().casefold(): index for index, name in enumerate(headers)}
        company = columns.get("компания", columns.get("company"))
        link = columns.get("ссылка", columns.get("url", columns.get("link")))
        tags = columns.get("теги", columns.get("tags"))
        if company is None or link is None:
            raise StoreError("В CSV нужны колонки «Компания» и «Ссылка».", 422)
        result = []
        for values in reader:
            if not values or not any(value.strip() for value in values):
                continue
            if len(result) >= MAX_ROWS:
                raise StoreError("CSV может содержать до 100 строк.", 422)
            result.append(preview_row(values, reader.line_num, company, link, tags, identities))
        return result
    except (UnicodeDecodeError, csv.Error):
        raise StoreError("Не удалось прочитать CSV в кодировке UTF-8.", 422) from None


def preview_row(values, number, company, link, tags, identities):
    if company >= len(values) or link >= len(values) or not values[link].strip():
        return {"row": number, "status": "invalid", "message": "Укажите карьерную страницу."}
    name, url = values[company].strip(), values[link].strip()
    try:
        if name:
            name = clean_text(name)
        if any(
            value.lstrip().startswith(("=", "+", "-", "@"))
            for index, value in enumerate(values)
            if index != link
        ):
            raise ValueError("Формулы в CSV не поддерживаются.")
        extras = re.split(r"[,;|]", values[tags]) if tags is not None and tags < len(values) else []
        spec = link_spec(url, name, [value.strip() for value in extras if value.strip()])
        identity = source_identity(spec.kind, spec.normalized())
    except (ValueError, TypeError) as error:
        return {"row": number, "status": "invalid", "message": str(error)}
    duplicate = identity in identities
    identities.add(identity)
    return {
        "row": number,
        "status": "duplicate" if duplicate else "new",
        "name": name,
        "spec": spec.model_dump(),
        "message": "Источник уже есть в списке." if duplicate else "Готов к добавлению.",
    }


def sheets_download_host(host):
    if host == "docs.google.com":
        return True
    prefix, suffix = "doc-", "-sheets.googleusercontent.com"
    if not host.startswith(prefix) or not host.endswith(suffix):
        return False
    label = host[len(prefix) : -len(suffix)]
    return bool(label) and SHEETS_DOWNLOAD_LABEL_CHARS.issuperset(label)


async def sheets_csv(value, factory=SourceClient):
    try:
        url = csv_export_url(value)
        async with factory() as client:
            for _ in range(3):
                response = await client.get(url)
                if not response.is_redirect:
                    response.raise_for_status()
                    media_type = response.headers.get("content-type", "").split(";")[0].casefold()
                    if media_type not in {"text/csv", "text/plain", "application/octet-stream"}:
                        raise ValueError("Таблица должна быть доступна публично как CSV.")
                    return response.content
                url = urljoin(url, response.headers.get("location", ""))
                target = urlsplit(url)
                if not sheets_download_host(target.hostname or ""):
                    raise ValueError("Экспорт перенаправил на неподдерживаемый адрес.")
            raise ValueError("Слишком много перенаправлений при экспорте таблицы.")
    except (httpx.HTTPError, SourceNetworkError):
        raise StoreError("Таблица недоступна. Проверьте публичный доступ к CSV.", 422) from None
    except ValueError as error:
        raise StoreError(str(error), 422) from None
