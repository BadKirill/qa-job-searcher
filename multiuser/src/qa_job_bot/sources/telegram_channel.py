from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

import httpx

from qa_job_bot.config import EUROPE_CIS_LOCATION_TERMS
from qa_job_bot.models import Job
from qa_job_bot.sources.base import JobSource, PartialSourceError
from qa_job_bot.utils import parse_datetime


class _MessageTextParser(HTMLParser):
    block_tags = {"br", "div", "li", "p", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def lines(self) -> list[str]:
        text = unescape("".join(self.parts))
        return [
            normalized
            for line in text.splitlines()
            if (normalized := re.sub(r"\s+", " ", line).strip())
        ]


class TelegramChannelSource(JobSource):
    remote_pattern = re.compile(r"\bremote\b|удал[её]н|удал[её]нка|дистанцион", re.IGNORECASE)
    hybrid_pattern = re.compile(r"\bhybrid\b|гибрид", re.IGNORECASE)
    location_labels = re.compile(
        r"location|локац|гео|формат|релокац|remote|удал[её]н|гибрид|hybrid",
        re.IGNORECASE,
    )
    location_aliases = {
        "по всему миру": "Worldwide",
        "весь мир": "Worldwide",
        "мир": "Worldwide",
        "глобально": "Worldwide",
        "worldwide": "Worldwide",
        "anywhere": "Anywhere",
        "европа": "Europe",
        "снг": "CIS",
        "сербия": "Serbia",
        "белград": "Belgrade",
        "россия": "Russia",
        "рф": "Russia",
        "казахстан": "Kazakhstan",
        "армения": "Armenia",
        "грузия": "Georgia",
        "украина": "Ukraine",
        "беларусь": "Belarus",
        "молдова": "Moldova",
        "польша": "Poland",
        "румыния": "Romania",
        "болгария": "Bulgaria",
        "черногория": "Montenegro",
        "кипр": "Cyprus",
        "турция": "Turkey",
        "германия": "Germany",
        "франция": "France",
        "испания": "Spain",
        "португалия": "Portugal",
        "нидерланды": "Netherlands",
        "великобритания": "United Kingdom",
    }

    def __init__(self, handle: str, role_keywords=()) -> None:
        self.handle = handle
        self.role_keywords = role_keywords
        self.name = f"Telegram · @{handle}"
        self.endpoint = f"https://t.me/s/{handle}"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        response = await client.get(self.endpoint)
        response.raise_for_status()
        jobs, oldest_id = self._parse_page(response.text)

        if oldest_id is not None:
            try:
                older = await client.get(self.endpoint, params={"before": oldest_id})
                older.raise_for_status()
                older_jobs, _ = self._parse_page(older.text)
                jobs.update(older_jobs)
            except Exception as error:
                if jobs:
                    raise PartialSourceError(list(jobs.values()), error) from error
                raise
        return list(jobs.values())

    def _parse_page(self, html: str) -> tuple[dict[str, Job], int | None]:
        blocks = re.findall(
            r'(<div class="tgme_widget_message_wrap[^>]*>.*?)'
            r'(?=<div class="tgme_widget_message_wrap|<div class="tgme_channel_info|</body>)',
            html,
            re.DOTALL,
        )
        jobs: dict[str, Job] = {}
        message_ids: list[int] = []
        for block in blocks:
            post_match = re.search(r'data-post="([^"]+)/(\d+)"', block)
            text_match = re.search(
                r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>',
                block,
                re.DOTALL,
            )
            if not post_match or not text_match:
                continue
            canonical_handle, message_id = post_match.groups()
            message_ids.append(int(message_id))
            lines = self._html_lines(text_match.group(1))
            title = self._title(lines)
            if not title:
                continue
            full_text = "\n".join(lines)
            headline_text = full_text
            source_id = f"{canonical_handle}/{message_id}"
            datetime_match = re.search(r'<time[^>]+datetime="([^"]+)"', block)
            workplace = ""
            if self.hybrid_pattern.search(headline_text):
                workplace = "hybrid"
            elif self.remote_pattern.search(headline_text):
                workplace = "remote"
            jobs[source_id] = Job(
                source=self.name,
                source_id=source_id,
                title=title,
                company=self._company(lines),
                location=self._location(lines),
                url=f"https://t.me/{source_id}",
                description=full_text,
                published_at=parse_datetime(datetime_match.group(1) if datetime_match else None),
                remote=(workplace == "remote") if workplace else None,
                workplace_type=workplace,
                salary=self._salary(lines),
                tags=("telegram",),
            )
        return jobs, min(message_ids, default=None)

    @staticmethod
    def _html_lines(value: str) -> list[str]:
        parser = _MessageTextParser()
        parser.feed(value)
        return parser.lines()

    def _title(self, lines: list[str]) -> str:
        if lines and re.search(r"дайджест|digest", lines[0], re.I):
            return ""
        candidates = [
            line
            for line in lines[:20]
            if 3 < len(line) <= 240
            and not line.startswith(("http://", "https://", "#"))
            and not re.match(r"requirements|требования|задачи", line, re.I)
            and line.casefold() not in {"тестировщик", "qa", "вакансия"}
        ]
        if candidates and re.fullmatch(r"we are hiring|hiring|ищем|вакансии?", candidates[0], re.I):
            return next(
                (
                    line
                    for line in candidates[1:4]
                    if any(term.casefold() in line.casefold() for term in self.role_keywords)
                ),
                candidates[0],
            )
        return candidates[0] if candidates else ""

    def _company(self, lines: list[str]) -> str:
        pattern = re.compile(
            r"^[^A-Za-zА-Яа-яЁё0-9]{0,4}(?:компания|company|работодатель)"
            r"\s*[:—-]\s*(.+)$",
            re.I,
        )
        for line in lines:
            if match := pattern.search(line):
                return match.group(1)[:120]
        return f"@{self.handle}"

    def _location(self, lines: list[str]) -> str:
        def has_location(line: str) -> bool:
            normalized = line.casefold()
            return bool(
                self.location_labels.search(line)
                or any(
                    re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized)
                    for term in (*self.location_aliases, *EUROPE_CIS_LOCATION_TERMS)
                )
            )

        candidates = [line for line in lines if has_location(line)]
        text = " · ".join(candidates)
        normalized = text.casefold()
        canonical = [
            label
            for term, label in self.location_aliases.items()
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized)
        ]
        canonical = list(dict.fromkeys(canonical))
        if canonical:
            prefix = (
                "Hybrid"
                if self.hybrid_pattern.search(text)
                else "Remote"
                if self.remote_pattern.search(text)
                else "Location"
            )
            return f"{prefix} · {', '.join(canonical)} · {text}"
        return text or "Not specified"

    @staticmethod
    def _salary(lines: list[str]) -> str:
        currency = re.compile(r"(?:\$|€|₽|USD|EUR|RUB|дин|RSD)", re.I)
        for line in lines:
            if currency.search(line) and re.search(r"\d", line):
                return line[:160]
        return ""
