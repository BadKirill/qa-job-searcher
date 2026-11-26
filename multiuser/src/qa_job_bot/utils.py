from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    block_tags = {
        "br",
        "dd",
        "div",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "tr",
    }

    ignored = {"script", "style", "template", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if (
            self.hidden
            or tag in self.ignored
            or ("hidden", None) in attrs
            or ("aria-hidden", "true") in attrs
        ):
            if tag not in {"br", "img", "hr", "input", "meta", "link", "wbr", "source"}:
                self.hidden.append(tag)
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    lines = [
        normalized
        for line in unescape(text).splitlines()
        if (normalized := re.sub(r"\s+", " ", line).strip())
    ]
    return "\n".join(lines)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
