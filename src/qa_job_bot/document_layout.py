import math
import re
from pathlib import Path

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ALIGNMENTS = {"left": 0, "center": 1, "right": 2}
ROLES = ("title", "header", "heading", "entry", "body", "italic")


def bounded(value, lower, upper):
    number = float(value)
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError("Unsupported template typography")
    return number


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Invalid template color")
    return value


def monochrome(value, maximum=96):
    checked = color(value)
    channels = [int(checked[offset : offset + 2], 16) for offset in (1, 3, 5)]
    shade = min(channels[0], maximum) if len(set(channels)) == 1 else maximum
    return "#" + f"{shade:02X}" * 3


def cover_letter_style():
    body = {
        "font_name": "Arial",
        "font_size": 10.2,
        "line_spacing": 14.2,
        "space_before": 0,
        "space_after": 10,
        "bold": False,
        "italic": False,
        "color": "#606060",
        "alignment": "left",
    }
    return {
        "font_size": 10.2,
        "page_width": 595.2756,
        "page_height": 841.8898,
        "margin_left": 68.3622,
        "margin_right": 68.3622,
        "margin_top": 60,
        "margin_bottom": 60,
        "typography": {
            "body": body,
            "entry": body.copy(),
            "italic": body.copy(),
            "title": body
            | {
                "font_size": 19,
                "line_spacing": 23,
                "space_after": 1,
                "bold": True,
                "color": "#1A1A1A",
                "alignment": "center",
            },
            "header": body
            | {
                "font_size": 8.8,
                "line_spacing": 10.8,
                "space_after": 7,
                "alignment": "center",
            },
            "heading": body
            | {
                "font_size": 14,
                "line_spacing": 18,
                "space_after": 10,
                "bold": True,
                "color": "#1A1A1A",
                "rule": {"color": "#B5B5B5", "width": 0.55},
            },
        },
    }


def typography(style, geometry):
    body_size = geometry["font_size"]
    result = {}
    for role in ROLES:
        observed = style.get("typography", {})
        raw = observed.get(role, observed.get("body", {}) if role in {"header", "italic"} else {})
        size = {"title": 22, "heading": 13}.get(role, body_size)
        alignment = raw.get("alignment", "left")
        if alignment not in ALIGNMENTS:
            raise ValueError("Invalid template alignment")
        result[role] = {
            "font_name": str(raw.get("font_name", style.get("font_name", "Arial")))[:120],
            "font_size": bounded(raw.get("font_size", size), 7, 32),
            "line_spacing": bounded(raw.get("line_spacing", size * 1.4), 7, 50),
            "space_before": bounded(raw.get("space_before", 12 if role == "heading" else 0), 0, 40),
            "space_after": bounded(raw.get("space_after", 9), 0, 40),
            "bold": bool(raw.get("bold", role in {"title", "heading", "entry"})),
            "italic": bool(raw.get("italic", role == "italic")),
            "color": monochrome(raw.get("color", "#000000")),
            "alignment": alignment,
        }
        if rule := raw.get("rule"):
            result[role]["rule"] = {
                "color": monochrome(rule["color"], 181),
                "width": bounded(rule["width"], 0.1, 3),
            }
    return result


def pdf_face(spec, unicode_font, text):
    family = spec["font_name"].lower()
    try:
        text.encode("cp1252")
        standard = next(
            (
                name
                for fragment, name in (
                    ("helvetica", "Helvetica"),
                    ("times", "Times"),
                    ("courier", "Courier"),
                )
                if fragment in family
            ),
            None,
        )
    except UnicodeEncodeError:
        standard = None
    bold, italic = spec["bold"], spec["italic"]
    if standard:
        if standard == "Times":
            return {
                (False, False): "Times-Roman",
                (True, False): "Times-Bold",
                (False, True): "Times-Italic",
                (True, True): "Times-BoldItalic",
            }[bold, italic]
        suffix = (
            "-BoldOblique" if bold and italic else "-Bold" if bold else "-Oblique" if italic else ""
        )
        return standard + suffix
    if bold or italic:
        variant = "BoldOblique" if bold and italic else "Bold" if bold else "Oblique"
        arial = "Bold Italic" if bold and italic else "Bold" if bold else "Italic"
        candidates = (
            f"/usr/share/fonts/truetype/dejavu/DejaVuSans-{variant}.ttf",
            f"/System/Library/Fonts/Supplemental/Arial {arial}.ttf",
        )
        for path in candidates:
            if Path(path).is_file():
                name = "Candidate" + variant
                pdfmetrics.registerFont(TTFont(name, path))
                return name
    return unicode_font


def pdf_style(role, spec, unicode_font, text):
    return ParagraphStyle(
        role,
        fontName=pdf_face(spec, unicode_font, text),
        fontSize=spec["font_size"],
        leading=spec["line_spacing"],
        spaceBefore=spec["space_before"],
        spaceAfter=spec["space_after"],
        alignment=ALIGNMENTS[spec["alignment"]],
        textColor=HexColor(spec["color"]),
        keepWithNext=role in {"title", "header", "heading", "entry", "italic"},
        allowWidows=0,
        allowOrphans=0,
    )


def docx_style(style, spec, keep_with_next=False):
    font = style.font
    font.name = spec["font_name"].split("+")[-1]
    font.size = Pt(spec["font_size"])
    font.bold, font.italic = spec["bold"], spec["italic"]
    font.color.rgb = RGBColor.from_string(spec["color"][1:])
    layout = style.paragraph_format
    layout.alignment = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }[spec["alignment"]]
    layout.line_spacing = Pt(spec["line_spacing"])
    layout.space_before = Pt(spec["space_before"])
    layout.space_after = Pt(spec["space_after"])
    layout.keep_with_next = keep_with_next


def docx_rule(paragraph, rule):
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for name, value in {
        "val": "single",
        "sz": str(round(rule["width"] * 8)),
        "color": rule["color"][1:],
        "space": "2",
    }.items():
        bottom.set(qn("w:" + name), value)
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)
