from __future__ import annotations

import base64
import io
import math
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from statistics import median

import pdfplumber
import pypdfium2 as pdfium
from pdfminer.pdftypes import resolve1

MAX_REFERENCE_PAGES = 20
MAX_REFERENCE_SIDE = 1400
MAX_REFERENCE_PIXELS = 30_000_000
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
BULLET = re.compile(r"^(?:[•◦▪●‣⁃–-]|\d+[.)])\s+")


class PdfTemplateError(ValueError):
    pass


@dataclass
class TextLine:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    font_name: str
    font_size: float
    bold: bool
    italic: bool
    color: str
    baseline: float | None = None
    page: int = 1
    kind: str = "body"
    alignment: str = "left"


def _color(value) -> str:
    parts = (value,) if isinstance(value, int | float) else tuple(value or (0,))
    if len(parts) == 4:
        c, m, y, k = parts
        parts = tuple((1 - v) * (1 - k) for v in (c, m, y))
    if len(parts) == 1:
        parts *= 3
    if len(parts) != 3 or not all(isinstance(v, int | float) and math.isfinite(v) for v in parts):
        raise PdfTemplateError("Unsupported PDF color")
    return "#" + "".join(f"{round(max(0, min(1, v)) * 255):02X}" for v in parts)


def _font_name(value: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", value)
    for family in ("Arial", "Calibri", "Helvetica", "Times", "Courier", "DejaVuSans"):
        if name.casefold().startswith(family.casefold()):
            return family
    return re.sub(r"[-,]?(?:BoldItalic|BoldOblique|Bold|Italic|Oblique|Regular|MT)$", "", name)


def _line(words: list[dict], page_height: float, page_number: int) -> TextLine:
    chars = [char for word in words for char in word["chars"] if char["text"].strip()]
    font, size, color = Counter(
        (char["fontname"], round(char["size"], 2), _color(char.get("non_stroking_color")))
        for char in chars
    ).most_common(1)[0][0]
    return TextLine(
        text=" ".join(word["text"] for word in words),
        x0=min(word["x0"] for word in words),
        x1=max(word["x1"] for word in words),
        top=min(word["top"] for word in words),
        bottom=max(word["bottom"] for word in words),
        font_name=_font_name(font),
        font_size=size,
        bold="bold" in font.casefold(),
        italic=bool(re.search("italic|oblique", font, re.I)),
        color=color,
        baseline=median(page_height - char["matrix"][5] for char in chars),
        page=page_number,
    )


def _page_lines(page) -> list[TextLine]:
    chars = page.chars
    if any(char["text"].startswith("(cid:") for char in chars):
        fonts = {
            getattr(font.get("BaseFont"), "name", "")
            for value in resolve1(page.page_obj.resources.get("Font", {})).values()
            if isinstance(font := resolve1(value), dict)
            and getattr(font.get("Encoding"), "name", None) == "WinAnsiEncoding"
        }
        chars = [
            {**char, "text": "•"}
            if char["text"] == "(cid:127)" and char["fontname"] in fonts
            else char
            for char in chars
        ]
        if any(char["text"].startswith("(cid:") for char in chars):
            raise PdfTemplateError("PDF font has unmapped characters; needs_input: upload DOCX")
    words = pdfplumber.utils.extract_words(
        chars,
        x_tolerance=1.5,
        y_tolerance=2,
        return_chars=True,
    )
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        if rows and abs(word["bottom"] - rows[-1][0]["bottom"]) <= 2.5:
            rows[-1].append(word)
        else:
            rows.append([word])
    columns = 0
    lines = []
    for row in rows:
        row.sort(key=lambda item: item["x0"])
        for index in range(1, len(row)):
            if (
                row[index]["x0"] - row[index - 1]["x1"] > 30
                and row[index - 1]["x1"] - row[0]["x0"] > page.width * 0.2
                and row[-1]["x1"] - row[index]["x0"] > page.width * 0.2
            ):
                columns += 1
                break
        lines.append(_line(row, page.height, page.page_number))
    left = [
        line
        for line in lines
        if line.x1 < page.width * 0.55 and line.x1 - line.x0 > page.width * 0.2
    ]
    right = [
        line
        for line in lines
        if line.x0 > page.width * 0.45 and line.x1 - line.x0 > page.width * 0.2
    ]
    overlap = (
        min(left[-1].bottom, right[-1].bottom) - max(left[0].top, right[0].top)
        if len(left) >= 2 and len(right) >= 2
        else 0
    )
    if columns >= 2 or overlap > 20:
        raise PdfTemplateError("Multi-column PDF needs_input: upload a single-column CV or DOCX")
    return lines


def _classify(lines: list[TextLine], page, body_size: float, title_size: float, page_number: int):
    left = min(line.x0 for line in lines)
    rules = [
        line
        for line in page.lines
        if abs(line["top"] - line["bottom"]) < 2 and line["x1"] - line["x0"] > page.width * 0.4
    ]
    before_heading = page_number == 1
    for line in lines:
        width = line.x1 - line.x0
        if abs((line.x0 + line.x1 - page.width) / 2) < 12 and line.x0 > left + 10:
            line.alignment = "center"
        elif (
            line.x0 > left + 10
            and abs(line.x1 - max(item.x1 for item in lines)) < 6
            and width < page.width * 0.4
        ):
            line.alignment = "right"
        has_rule = any(0 <= rule["top"] - line.bottom < 14 for rule in rules)
        if line.font_size >= title_size - 0.2 and line.font_size > body_size * 1.5:
            line.kind = "title"
        elif line.bold and (has_rule or line.font_size >= body_size * 1.2):
            line.kind, before_heading = "heading", False
        elif before_heading and line.alignment == "center":
            line.kind = "contact"
        elif BULLET.match(line.text):
            line.kind = "bullet"
        elif line.italic:
            line.kind = "role"
        elif line.bold:
            line.kind = "entry"
    return rules


def _can_join(previous: TextLine, current: TextLine, kind: str) -> bool:
    same_font = (
        current.font_name == previous.font_name
        and abs(current.font_size - previous.font_size) < 0.3
        and current.bold == previous.bold
        and current.italic == previous.italic
        and current.color == previous.color
    )
    if current.kind != "body" or kind not in {"body", "bullet"} or not same_font:
        return False
    gap = current.top - previous.bottom
    if not -1 <= gap <= previous.font_size * 0.45:
        return False
    indent = current.x0 - previous.x0
    return abs(indent) <= 2 if kind == "body" else -2 <= indent <= previous.font_size * 2.5


def _paragraphs(lines: list[TextLine], page_number: int) -> list[dict]:
    blocks: list[dict] = []
    previous = None
    for line in lines:
        if previous and _can_join(previous, line, blocks[-1]["kind"]):
            suffix = blocks[-1]["text"]
            separator = "" if suffix.endswith("\u00ad") or re.search(r"\w-$", suffix) else " "
            blocks[-1]["text"] = blocks[-1]["text"].rstrip("\u00ad") + separator + line.text
        else:
            blocks.append(
                {
                    "text": line.text,
                    "kind": line.kind,
                    "source": f"page:{page_number}:block:{len(blocks) + 1}",
                    "appearance": {
                        key: getattr(line, key)
                        for key in (
                            "font_name",
                            "font_size",
                            "bold",
                            "italic",
                            "color",
                            "alignment",
                        )
                    },
                }
            )
        previous = line
    return blocks


def _typography(lines: list[TextLine], rules: list[dict], body_size: float) -> dict:
    typography = {}
    for role, kinds in {
        "title": {"title"},
        "header": {"contact"},
        "heading": {"heading"},
        "entry": {"entry"},
        "body": {"body", "bullet"},
        "italic": {"role"},
    }.items():
        choices = [line for line in lines if line.kind in kinds]
        if not choices:
            continue
        line = max(
            choices,
            key=lambda item: sum(
                len(other.text)
                for other in choices
                if (other.font_name, other.font_size, other.color)
                == (item.font_name, item.font_size, item.color)
            ),
        )
        gaps = [
            _baseline(current) - _baseline(previous)
            for previous, current in zip(lines, lines[1:], strict=False)
            if previous.page == current.page
            and previous.kind in kinds
            and current.kind in kinds
            and _can_join(previous, current, previous.kind)
            and previous.font_size
            <= _baseline(current) - _baseline(previous)
            <= previous.font_size * 1.7
        ]
        typography[role] = {
            "font_name": line.font_name,
            "font_size": line.font_size,
            "bold": line.bold,
            "italic": line.italic,
            "color": line.color,
            "alignment": line.alignment,
            "line_spacing": round(median(gaps), 2) if gaps else round(line.font_size * 1.25, 2),
            "space_before": 0,
            "space_after": 0,
        }
    if rules and "heading" in typography:
        typography["heading"]["rule"] = {
            "color": _color(rules[0].get("stroking_color")),
            "width": max(0.2, min(3, float(rules[0].get("linewidth") or 0.5))),
        }
    _block_spacing(lines, typography)
    return typography


def _baseline(line: TextLine) -> float:
    return line.baseline if line.baseline is not None else line.top + line.font_size * 0.8


def _block_spacing(lines: list[TextLine], typography: dict) -> None:
    samples = {
        (role, spacing): [] for role in typography for spacing in ("space_before", "space_after")
    }
    aliases = {"contact": "header", "role": "italic", "bullet": "body"}
    block_kind = lines[0].kind
    for previous, current in zip(lines, lines[1:], strict=False):
        if previous.page != current.page:
            block_kind = current.kind
            continue
        if _can_join(previous, current, block_kind):
            continue
        block_kind = current.kind
        before = aliases.get(previous.kind, previous.kind)
        after = aliases.get(current.kind, current.kind)
        extra = (
            _baseline(current)
            - _baseline(previous)
            - typography[before]["line_spacing"]
            + previous.font_size
            - current.font_size
        )
        if after in {"heading", "entry"}:
            key = (after, "space_before")
        else:
            key = (before, "space_after")
            if before == "heading" and "rule" in typography[before]:
                extra -= 3
        samples[key].append(max(0, min(40, extra)))
    for (role, spacing), values in samples.items():
        if values:
            typography[role][spacing] = round(median(values), 2)


def render_reference(content: bytes, dimensions: list[tuple[float, float]]) -> dict:
    if not 0 < len(dimensions) <= MAX_REFERENCE_PAGES:
        raise PdfTemplateError("PDF preview page limit exceeded")
    if any(
        not math.isfinite(value) or not 72 <= value <= 14_400
        for dimension in dimensions
        for value in dimension
    ):
        raise PdfTemplateError("Unsupported PDF preview geometry")
    pages, pixels, total_bytes = [], 0, 0
    with pdfium.PdfDocument(content) as document:
        if len(document) != len(dimensions):
            raise PdfTemplateError("PDF parsers disagree on page count")
        for index, (width, height) in enumerate(dimensions):
            scale = min(2, MAX_REFERENCE_SIDE / max(width, height))
            pixels += math.ceil(width * scale) * math.ceil(height * scale)
            if pixels > MAX_REFERENCE_PIXELS:
                raise PdfTemplateError("PDF preview pixel limit exceeded")
            page = document[index]
            try:
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    image_width, image_height = image.size
                    image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
            png = output.getvalue()
            total_bytes += len(png)
            if total_bytes > MAX_REFERENCE_BYTES:
                raise PdfTemplateError("PDF preview byte limit exceeded")
            pages.append(
                {
                    "page": index + 1,
                    "width": image_width,
                    "height": image_height,
                    "image_base64": base64.b64encode(png).decode(),
                    "image_sha256": sha256(png).hexdigest(),
                }
            )
    return {"sha256": sha256(content).hexdigest(), "page_count": len(pages), "pages": pages}


def extract_pdf_template(content: bytes, max_pages: int = 20, max_text: int = 100_000) -> dict:
    blocks, all_lines, all_rules, dimensions = [], [], [], []
    with pdfplumber.open(io.BytesIO(content)) as document:
        if not 0 < len(document.pages) <= min(max_pages, MAX_REFERENCE_PAGES):
            raise PdfTemplateError("PDF page limit exceeded")
        pages = []
        character_count = 0
        for page in document.pages:
            width, height = float(page.width), float(page.height)
            if not all(math.isfinite(value) and 72 <= value <= 14_400 for value in (width, height)):
                raise PdfTemplateError("Unsupported PDF page geometry")
            dimensions.append((width, height))
            character_count += len(page.chars)
            if character_count > max_text:
                raise PdfTemplateError("PDF has too much text")
            lines = _page_lines(page)
            if not lines:
                raise PdfTemplateError(
                    "PDF is empty or scanned; needs_input: upload selectable text"
                )
            pages.append((page, lines))
            all_lines.extend(lines)
        body_size = Counter(
            {
                size: sum(len(line.text) for line in all_lines if line.font_size == size)
                for size in {line.font_size for line in all_lines}
            }
        ).most_common(1)[0][0]
        title_size = max(line.font_size for line in all_lines)
        for index, (page, lines) in enumerate(pages, 1):
            all_rules.extend(_classify(lines, page, body_size, title_size, index))
            blocks.extend(_paragraphs(lines, index))
        width, height = dimensions[0]
        style = {
            "source_format": "pdf",
            "layout_fidelity": "visual_reference",
            "page_width": width,
            "page_height": height,
            "margin_left": round(min(line.x0 for line in all_lines), 2),
            "margin_right": round(
                min(page.width - line.x1 for page, lines in pages for line in lines), 2
            ),
            "margin_top": round(min(_baseline(line) - line.font_size for line in all_lines), 2),
            "margin_bottom": round(
                min(page.height - line.bottom for page, lines in pages for line in lines), 2
            ),
            "typography": _typography(all_lines, all_rules, body_size),
            "section_order": [line.text for line in all_lines if line.kind == "heading"],
        }
        body = style["typography"].get("body") or next(iter(style["typography"].values()))
        style.update(font_name=body["font_name"], font_size=body["font_size"])
        for name in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            style[name] = max(24, min(110, style[name]))
    if len(blocks) > 500 or sum(len(block["text"]) for block in blocks) > max_text:
        raise PdfTemplateError("PDF has too much text")
    style["paragraph_typography"] = {
        f"fact-{index}": block.pop("appearance", {}) for index, block in enumerate(blocks, 1)
    }
    return {
        "extracted_text": "\n".join(block["text"] for block in blocks),
        "facts": [{"id": f"fact-{index}", **block} for index, block in enumerate(blocks, 1)],
        "style": style,
        "reference": render_reference(content, dimensions),
    }
