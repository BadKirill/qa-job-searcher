import io
import json
import os
import re
import resource
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.shared import Pt
from pypdf import PdfReader, apply_configuration
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, HRFlowable, PageTemplate, Paragraph, Spacer

from qa_job_bot.document_layout import (
    cover_letter_style,
    docx_rule,
    docx_style,
    pdf_style,
    typography,
)
from qa_job_bot.document_policy import Draft, canonical_text
from qa_job_bot.pdf_template import PdfTemplateError, extract_pdf_template

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_PAGES = 20
MAX_TEXT = 100_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


class DocumentFileError(ValueError):
    pass


def _xml(payload: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
        raise DocumentFileError("XML entities are not permitted")
    return ElementTree.fromstring(payload)


def _docx(content: bytes) -> tuple[str, dict]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 250 or sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
            raise DocumentFileError("DOCX archive exceeds safe limits")
        for item in entries:
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in item.filename:
                raise DocumentFileError("Invalid archive path")
            if item.flag_bits & 1 or item.file_size > 10_000_000:
                raise DocumentFileError("Encrypted or oversized archive entry")
            if any(
                word in item.filename.lower() for word in ("vbaproject", "embeddings/", "activex")
            ):
                raise DocumentFileError("Active DOCX content is not permitted")
            if item.filename.endswith((".xml", ".rels")):
                node = _xml(archive.read(item))
                for element in node.iter():
                    tag = element.tag.rsplit("}", 1)[-1]
                    if tag in {"vanish", "webHidden", "instrText", "altChunk", "object"}:
                        raise DocumentFileError(
                            "Hidden or executable DOCX content is not permitted"
                        )
                    if element.get("TargetMode") == "External":
                        if not element.get("Type", "").endswith("/hyperlink"):
                            raise DocumentFileError("External DOCX resources are not permitted")
                        if not re.match(r"^(https?://|mailto:)", element.get("Target", "")):
                            raise DocumentFileError("Unsafe hyperlink")
        root = _xml(archive.read("word/document.xml"))
        paragraphs = [
            "".join(
                "\n" if node.tag == f"{W}br" else (node.text or "")
                for node in paragraph.iter()
                if node.tag in {f"{W}t", f"{W}br"}
            )
            for paragraph in root.iter(f"{W}p")
        ]
        document = Document(io.BytesIO(content))
        section = document.sections[0]
        normal = document.styles["Normal"].font
        style = {
            "font_name": normal.name or "Arial",
            "font_size": float(normal.size.pt) if normal.size else 11.0,
            "page_width": float(section.page_width.pt),
            "page_height": float(section.page_height.pt),
            "margin_top": float(section.top_margin.pt),
            "margin_bottom": float(section.bottom_margin.pt),
            "margin_left": float(section.left_margin.pt),
            "margin_right": float(section.right_margin.pt),
            "source_format": "docx",
            "layout_fidelity": "typography_and_page_geometry",
        }
        return "\n".join(line for line in paragraphs if line.strip()), style


def _pdf(content: bytes) -> dict:
    with apply_configuration(
        maximum_declared_stream_length=MAX_UNCOMPRESSED_BYTES,
        zlib_maximum_output_length=MAX_UNCOMPRESSED_BYTES,
        lzw_maximum_output_length=MAX_UNCOMPRESSED_BYTES,
        run_length_maximum_output_length=MAX_UNCOMPRESSED_BYTES,
        array_based_stream_maximum_output_length=MAX_UNCOMPRESSED_BYTES,
        jbig2dec_binary=None,
    ):
        return _pdf_extract(content)


def _pdf_extract(content: bytes) -> dict:
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
        raise DocumentFileError("Encrypted PDF or page limit exceeded")
    forbidden = {"/JavaScript", "/JS", "/Launch", "/OpenAction", "/AA", "/EmbeddedFiles", "/XFA"}
    seen = set()
    image_pixels = 0
    pending = [reader.trailer]
    while pending:
        current = pending.pop().get_object()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > 100_000:
            raise DocumentFileError("PDF object limit exceeded")
        if isinstance(current, dict):
            if forbidden.intersection(current):
                raise DocumentFileError("Active PDF content is not permitted")
            if current.get("/Subtype") == "/Image":
                width, height = current.get("/Width"), current.get("/Height")
                if not all(
                    isinstance(value, int) and 0 < value <= 30_000_000 for value in (width, height)
                ):
                    raise DocumentFileError("Invalid PDF image dimensions")
                image_pixels += width * height
                if image_pixels > 30_000_000:
                    raise DocumentFileError("PDF source image pixel limit exceeded")
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    for page in reader.pages:
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > MAX_UNCOMPRESSED_BYTES:
            raise DocumentFileError("PDF stream limit exceeded")
    try:
        return extract_pdf_template(content, max_pages=MAX_PAGES, max_text=MAX_TEXT)
    except PdfTemplateError as error:
        raise DocumentFileError(str(error)) from error


def _parse_template(content: bytes, filename: str) -> dict:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise DocumentFileError("File is empty or exceeds 10 MiB")
    extension = Path(filename).suffix.casefold()
    try:
        if extension == ".docx" and content.startswith(b"PK"):
            text, style = _docx(content)
        elif extension == ".pdf" and content.startswith(b"%PDF-"):
            return _pdf(content)
        else:
            raise DocumentFileError("Upload a valid DOCX or PDF file")
    except DocumentFileError:
        raise
    except Exception as error:
        raise DocumentFileError("Document could not be parsed safely") from error
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(text) > MAX_TEXT or len(lines) > 500:
        raise DocumentFileError("Document is empty or has too much text")
    return {
        "extracted_text": "\n".join(lines),
        "facts": [
            {"id": f"fact-{index}", "text": line, "source": f"paragraph:{index}"}
            for index, line in enumerate(lines, 1)
        ],
        "style": style,
    }


def _geometry(style: dict) -> dict[str, float]:
    defaults = {
        "font_size": 11,
        "page_width": 595.28,
        "page_height": 841.89,
        "margin_top": 48,
        "margin_bottom": 48,
        "margin_left": 48,
        "margin_right": 48,
    }
    result = {}
    for key, default in defaults.items():
        value = float(style.get(key, default))
        lower, upper = (
            (7, 24)
            if key == "font_size"
            else ((400, 1000) if key.startswith("page") else (24, 110))
        )
        if not lower <= value <= upper:
            raise DocumentFileError("Unsupported template page geometry")
        result[key] = value
    return result


def _pdf_font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            pdfmetrics.registerFont(TTFont("CandidateUnicode", candidate))
            return "CandidateUnicode"
    raise DocumentFileError("A Unicode PDF font is required")


def effective_document_style(template: dict, document_kind: str) -> dict:
    if document_kind not in {"cv", "cover_letter"}:
        raise DocumentFileError("Unsupported document kind")
    style = cover_letter_style() if document_kind == "cover_letter" else template.get("style", {})
    geometry = _geometry(style)
    try:
        roles = typography(style, geometry)
    except (ValueError, TypeError, KeyError) as error:
        raise DocumentFileError("Unsupported template typography") from error
    for role in ("title", "header"):
        roles[role]["alignment"] = "center"
    return {"geometry": geometry, "typography": roles}


def _paragraph_override(template: dict, paragraph: dict, role: str, document_kind: str):
    if document_kind != "cv" or len(paragraph["fact_ids"]) != 1:
        return None
    fact_id = paragraph["fact_ids"][0]
    fact = next((item for item in template.get("facts", []) if item.get("id") == fact_id), None)
    if fact is None:
        return None
    source_role = {
        "body": "body",
        "bullet": "body",
        "entry": "entry",
        "role": "italic",
        "contact": "header",
        "heading": "heading",
        "title": "title",
    }.get(fact.get("kind", "body"))
    if source_role != role:
        return None
    return template.get("style", {}).get("paragraph_typography", {}).get(fact_id)


def render_documents(
    draft: dict, template: dict, output_dir: Path, document_kind: str = "cv"
) -> list[dict]:
    draft = Draft.model_validate(draft).model_dump()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    appearance = effective_document_style(template, document_kind)
    geometry, roles = appearance["geometry"], appearance["typography"]
    document = Document()
    section = document.sections[0]
    for key, value in geometry.items():
        if key.startswith("page"):
            setattr(section, key, Pt(value))
        elif key.startswith("margin_"):
            setattr(section, key.removeprefix("margin_") + "_margin", Pt(value))
    names = {"body": "Normal", "title": "Title", "heading": "Heading 1"}
    for role, spec in roles.items():
        name = names.setdefault(role, "CV " + role)
        if name not in document.styles:
            document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        docx_style(
            document.styles[name],
            spec,
            keep_with_next=role in {"title", "header", "heading", "entry", "italic"},
        )
    for border in document.styles["Title"].element.xpath(".//w:pBdr"):
        border.getparent().remove(border)
    document.add_paragraph(draft["title"], style="Title")
    pdf_font = _pdf_font()
    styles = {
        role: pdf_style(role, spec, pdf_font, canonical_text(draft)) for role, spec in roles.items()
    }
    styles["bullet"] = pdf_style("bullet", roles["body"], pdf_font, canonical_text(draft))
    styles["bullet"].leftIndent = geometry["font_size"] * 1.4
    styles["bullet"].firstLineIndent = -geometry["font_size"]
    story = [Paragraph(escape(draft["title"]), styles["title"])]
    in_cv_header = document_kind == "cv"
    for item in draft["sections"]:
        in_cv_header = in_cv_header and not item["heading"].strip()
        if item["heading"]:
            section_heading = document.add_paragraph(item["heading"], style="Heading 1")
            story.append(Paragraph(escape(item["heading"]), styles["heading"]))
            if rule := roles["heading"].get("rule"):
                docx_rule(section_heading, rule)
                line = HRFlowable(
                    width="100%",
                    thickness=rule["width"],
                    color=HexColor(rule["color"]),
                    spaceAfter=3,
                )
                line.keepWithNext = True
                story.append(line)
        for paragraph in item["paragraphs"]:
            kind = "contact" if in_cv_header else paragraph.get("kind", "body")
            role = {"contact": "header", "role": "italic", "bullet": "body"}.get(kind, kind)
            text = paragraph["text"]
            name = names[role]
            override = _paragraph_override(template, paragraph, role, document_kind)
            paragraph_style = styles["bullet" if kind == "bullet" else role]
            if override:
                try:
                    spec = typography({"typography": {role: roles[role] | override}}, geometry)[
                        role
                    ]
                except (ValueError, TypeError, KeyError) as error:
                    raise DocumentFileError("Unsupported paragraph typography") from error
                if role == "header":
                    spec["alignment"] = "center"
                name = "CV paragraph " + str(len(document.paragraphs))
                custom = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
                custom.base_style = document.styles[names[role]]
                docx_style(
                    custom,
                    spec,
                    keep_with_next=role in {"header", "heading", "entry", "italic"},
                )
                paragraph_style = pdf_style(role, spec, pdf_font, text)
                paragraph_style.leftIndent = styles[
                    "bullet" if kind == "bullet" else role
                ].leftIndent
                paragraph_style.firstLineIndent = styles[
                    "bullet" if kind == "bullet" else role
                ].firstLineIndent
            block = document.add_paragraph(text, style=name)
            if kind == "bullet":
                block.paragraph_format.left_indent = Pt(geometry["font_size"] * 1.4)
                block.paragraph_format.first_line_indent = Pt(-geometry["font_size"])
            story.append(
                Paragraph(
                    escape(text)
                    .replace("\n", "<br/>")
                    .replace("•", f'<font name="{pdf_font}">•</font>'),
                    paragraph_style,
                )
            )
    story.append(Spacer(1, 1))
    docx_path, pdf_path = output_dir / "document.docx", output_dir / "document.pdf"
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    document.save(docx_path)
    pdf = BaseDocTemplate(
        str(pdf_path),
        pagesize=(geometry["page_width"], geometry["page_height"]),
        topMargin=geometry["margin_top"],
        bottomMargin=geometry["margin_bottom"],
        leftMargin=geometry["margin_left"],
        rightMargin=geometry["margin_right"],
        title=draft["title"],
        author="",
    )
    frame = Frame(
        pdf.leftMargin,
        pdf.bottomMargin,
        pdf.width,
        pdf.height,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    pdf.addPageTemplates(PageTemplate(id="CV", frames=[frame]))
    pdf.build(story)
    expected = " ".join(canonical_text(draft).split())
    docx_text = " ".join("\n".join(p.text for p in Document(docx_path).paragraphs).split())
    pdf_text = " ".join("\n".join(p.extract_text() for p in PdfReader(pdf_path).pages).split())
    if docx_text != expected or pdf_text != expected:
        raise DocumentFileError("Rendered text differs from the approved document")
    for path in (docx_path, pdf_path):
        path.chmod(0o600)
    return [
        {"path": path, "format": path.suffix[1:], "filename": path.name}
        for path in (docx_path, pdf_path)
    ]


def parse_template(content: bytes, filename: str) -> dict:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise DocumentFileError("File is empty or exceeds 10 MiB")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "qa_job_bot.document_files", Path(filename).suffix.lower()],
            input=content,
            capture_output=True,
            timeout=15,
            check=False,
            cwd="/tmp",
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            },
        )
        payload = json.loads(result.stdout)
        if result.returncode or "error" in payload:
            raise DocumentFileError(payload.get("error", "Document parser failed"))
        return payload
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as error:
        raise DocumentFileError("Document parser failed or exceeded resource limits") from error


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    try:
        content = sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1)
        result = _parse_template(content, "template" + sys.argv[1])
    except (DocumentFileError, IndexError) as error:
        result = {"error": str(error)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
