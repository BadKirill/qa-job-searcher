from copy import deepcopy
from pathlib import Path

import pdfplumber
import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from pypdf import PdfReader

from qa_job_bot import document_layout as layout
from qa_job_bot.document_files import (
    DocumentFileError,
    _geometry,
    effective_document_style,
    parse_template,
    render_documents,
)
from qa_job_bot.document_policy import canonical_text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 33, "bad"])
def test_typography_rejects_invalid_and_unbounded_values(value):
    with pytest.raises(ValueError):
        layout.typography({"typography": {"body": {"font_size": value}}}, _geometry({}))


@pytest.mark.parametrize(
    "spec",
    [
        {"color": "red"},
        {"color": None},
        {"alignment": "justify"},
        {"rule": {"color": "#000000", "width": 5}},
    ],
)
def test_typography_rejects_unsupported_style(spec):
    with pytest.raises(ValueError):
        layout.typography({"typography": {"heading": spec}}, _geometry({}))


@pytest.mark.parametrize("family", ["Helvetica", "Times", "Courier"])
@pytest.mark.parametrize(
    "bold,italic", [(False, False), (True, False), (False, True), (True, True)]
)
def test_standard_pdf_fonts_keep_weight_and_slant(family, bold, italic):
    font = layout.pdf_face(
        {"font_name": family, "bold": bold, "italic": italic}, "Fallback", "Alex"
    )
    assert font.startswith(family)
    assert ("Bold" in font) == bold
    assert ("Italic" in font or "Oblique" in font) == italic


@pytest.mark.parametrize("bold,italic", [(True, False), (False, True), (True, True)])
def test_unicode_font_variants_and_missing_fallback(monkeypatch, bold, italic):
    spec = {"font_name": "Helvetica", "bold": bold, "italic": italic}
    monkeypatch.setattr(layout.Path, "is_file", lambda path: False)
    assert layout.pdf_face(spec, "Fallback", "Привет") == "Fallback"
    registrations = []
    monkeypatch.setattr(layout.Path, "is_file", lambda path: True)
    monkeypatch.setattr(layout, "TTFont", lambda name, path: (name, path))
    monkeypatch.setattr(layout.pdfmetrics, "registerFont", registrations.append)
    selected = layout.pdf_face(spec, "Fallback", "Привет")
    assert registrations[0][0] == selected
    assert selected.startswith("Candidate")


def test_role_styles_preserve_typography_and_docx_rule():
    source = {
        "font_name": "ABC+Arial",
        "font_size": 8.5,
        "alignment": "right",
        "color": "#606060",
        "bold": False,
        "italic": True,
        "line_spacing": 11,
        "space_before": 2,
        "space_after": 3,
        "rule": {"color": "#B5B5B5", "width": 0.5},
    }
    spec = layout.typography({"typography": {"heading": source}}, _geometry({}))["heading"]
    pdf = layout.pdf_style("heading", spec, "Helvetica", "Alex")
    assert pdf.alignment == 2 and pdf.fontSize == 8.5 and pdf.leading == 11
    assert pdf.keepWithNext and pdf.spaceBefore == 2 and pdf.spaceAfter == 3
    assert layout.pdf_style("italic", spec, "Helvetica", "Alex").keepWithNext
    doc = Document()
    layout.docx_style(doc.styles["Normal"], spec)
    style = doc.styles["Normal"]
    assert style.font.name == "Arial" and style.font.size.pt == 8.5
    assert style.font.italic and not style.font.bold
    assert str(style.font.color.rgb) == "606060"
    assert style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.RIGHT
    paragraph = doc.add_paragraph("Summary")
    layout.docx_rule(paragraph, spec["rule"])
    bottom = paragraph._p.find(".//" + qn("w:bottom"))
    assert bottom.get(qn("w:color")) == "B5B5B5" and bottom.get(qn("w:sz")) == "4"


def synthetic_draft(template):
    sections = []
    title = ""
    for fact in template["facts"]:
        kind = fact["kind"]
        if kind == "title":
            title = fact["text"]
        elif kind == "heading":
            sections.append({"heading": fact["text"], "paragraphs": []})
        else:
            if not sections:
                sections.append({"heading": "", "paragraphs": []})
            sections[-1]["paragraphs"].append(
                {"text": fact["text"], "kind": kind, "fact_ids": [fact["id"]]}
            )
    return {"title": title, "language": "en", "sections": sections}


def test_pdf_template_renders_semantic_roles_and_text_without_loss(tmp_path):
    template = parse_template(
        (Path(__file__).parent / "fixtures/candidate.pdf").read_bytes(), "candidate.pdf"
    )
    draft = synthetic_draft(template)
    files = render_documents(draft, template, tmp_path)
    pdf = PdfReader(next(file["path"] for file in files if file["format"] == "pdf"))
    assert len(pdf.pages) >= 1
    doc = Document(next(file["path"] for file in files if file["format"] == "docx"))
    assert doc.paragraphs[0].alignment is None
    assert doc.styles["Title"].paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert any(p.style.base_style.name == "CV entry" for p in doc.paragraphs)
    assert any(p.style.base_style.name == "CV italic" for p in doc.paragraphs)
    assert any(
        p.paragraph_format.first_line_indent and p.paragraph_format.first_line_indent.pt < 0
        for p in doc.paragraphs
    )
    assert doc.element.xpath(".//w:pBdr")


def test_renderer_rejects_invalid_typography_before_artifacts(tmp_path):
    draft = {
        "title": "Alex",
        "language": "en",
        "sections": [
            {"heading": "Skills", "paragraphs": [{"text": "Testing", "fact_ids": ["f1"]}]}
        ],
    }
    with pytest.raises(DocumentFileError, match="typography"):
        render_documents(
            draft,
            {"style": {"typography": {"body": {"color": "url(https://example.com)"}}}},
            tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid", [False, True])
def test_individual_contact_emphasis_and_invalid_overrides(tmp_path, invalid):
    draft = {
        "title": "Alex",
        "language": "en",
        "sections": [
            {
                "heading": "",
                "paragraphs": [
                    {"text": "Remote", "kind": "contact", "fact_ids": ["f1"]},
                    {"text": "QA Engineer", "kind": "contact", "fact_ids": ["f2"]},
                ],
            },
            {
                "heading": "Summary",
                "paragraphs": [{"text": "Testing", "fact_ids": ["f1", "f2"]}],
            },
        ],
    }
    template = {
        "facts": [
            {"id": "f1", "kind": "body"},
            {"id": "f2", "kind": "contact"},
        ],
        "style": {
            "paragraph_typography": {
                "f2": {
                    "bold": True,
                    "alignment": "center",
                    "color": "red" if invalid else "#606060",
                }
            }
        },
    }
    if invalid:
        with pytest.raises(DocumentFileError, match="paragraph typography"):
            render_documents(draft, template, tmp_path)
        assert list(tmp_path.iterdir()) == []
    else:
        files = render_documents(draft, template, tmp_path)
        doc = Document(files[0]["path"])
        assert not doc.paragraphs[1].style.font.bold
        assert doc.paragraphs[2].style.font.bold
        assert doc.paragraphs[2].style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
        assert str(doc.paragraphs[2].style.font.color.rgb) == "606060"
        assert doc.paragraphs[4].style.name == "Normal"


@pytest.mark.parametrize(
    "kind,fact_ids,alignment",
    [
        (None, ["f1"], "left"),
        ("body", ["f1"], "right"),
        ("role", ["f1", "f2"], "left"),
        ("contact", ["f1"], "left"),
        ("bullet", ["f1"], "right"),
        ("entry", ["f1"], "left"),
    ],
)
def test_cv_header_centers_wrapped_headline_despite_model_kind_and_overrides(
    tmp_path, kind, fact_ids, alignment
):
    headline = {
        "text": (
            "QA Lead | Quality Engineering & Test Automation | Python, Pytest & Playwright "
            "| API Testing, Continuous Integration and Delivery | Manual and Automated Testing"
        ),
        "fact_ids": fact_ids,
    }
    if kind is not None:
        headline["kind"] = kind
    draft = {
        "title": "Alex Example",
        "language": "en",
        "sections": [
            {
                "heading": "",
                "paragraphs": [{"text": "Remote", "kind": "contact", "fact_ids": ["f2"]}],
            },
            {"heading": "", "paragraphs": [headline]},
            {
                "heading": "Summary",
                "paragraphs": [{"text": "I test software.", "fact_ids": ["f1"]}],
            },
            {
                "heading": "",
                "paragraphs": [{"text": "QA Engineer", "kind": "role", "fact_ids": ["f1"]}],
            },
        ],
    }
    template = {
        "facts": [{"id": "f1", "kind": "contact"}, {"id": "f2", "kind": "body"}],
        "style": {
            "margin_left": 72,
            "margin_right": 48,
            "typography": {
                "title": {"alignment": "left"},
                "header": {"alignment": "right"},
            },
            "paragraph_typography": {"f1": {"alignment": alignment, "bold": True}},
        },
    }
    original_draft, original_template = deepcopy(draft), deepcopy(template)
    files = render_documents(draft, template, tmp_path)
    doc = Document(files[0]["path"])
    for paragraph in doc.paragraphs[:3]:
        assert paragraph.style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
        assert paragraph.paragraph_format.left_indent is None
        assert paragraph.paragraph_format.first_line_indent is None
    assert doc.paragraphs[2].style.font.bold == (len(fact_ids) == 1)
    for paragraph in doc.paragraphs[3:]:
        assert paragraph.style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.LEFT
    with pdfplumber.open(files[1]["path"]) as pdf:
        page = pdf.pages[0]
        lines = page.extract_text_lines()
        summary = next(index for index, line in enumerate(lines) if line["text"] == "Summary")
        assert summary >= 4
        center = (page.width + 72 - 48) / 2
        for line in lines[:summary]:
            assert (line["x0"] + line["x1"]) / 2 == pytest.approx(center, abs=0.05)
        for line in lines[summary:]:
            assert line["x0"] == pytest.approx(72, abs=0.05)
    style = effective_document_style(template, "cv")
    assert style["typography"]["header"]["alignment"] == "center"
    assert style["typography"]["title"]["alignment"] == "center"
    assert draft == original_draft and template == original_template


def test_cover_letter_uses_safe_spacing_without_fact_source_style_overrides(tmp_path):
    draft = {
        "title": "Alex — QA Engineer",
        "language": "en",
        "sections": [
            {
                "heading": "",
                "paragraphs": [
                    {"text": "Dear Hiring Team,", "fact_ids": ["f1"]},
                    {"text": "I test APIs.", "fact_ids": ["f2"]},
                ],
            }
        ],
    }
    template = {
        "facts": [
            {"id": "f1", "kind": "title"},
            {"id": "f2", "kind": "body"},
        ],
        "style": {
            "typography": {
                "title": {"space_after": 0},
                "body": {"space_after": 0},
            },
            "paragraph_typography": {"f1": {"font_size": 24, "bold": True, "alignment": "center"}},
        },
    }
    files = render_documents(draft, template, tmp_path, "cover_letter")
    doc = Document(files[0]["path"])
    assert doc.styles["Title"].paragraph_format.space_after.pt == 1
    assert doc.styles["Normal"].paragraph_format.space_after.pt == 10
    assert doc.paragraphs[1].style.name == "Normal"
    assert doc.paragraphs[1].style.font.size.pt != 24


@pytest.mark.parametrize(
    "color,expected",
    [
        ("#FF0000", "#606060"),
        ("#000000", "#000000"),
        ("#606060", "#606060"),
        ("#FFFFFF", "#606060"),
        ("#1a1a1a", "#1A1A1A"),
    ],
)
def test_text_colors_are_monochrome_and_readable(color, expected):
    assert layout.monochrome(color) == expected
    assert layout.monochrome("#FFFFFF", 181) == "#B5B5B5"


def test_cover_style_is_fixed_and_does_not_modify_original():
    template = {"style": {"font_size": 24, "margin_left": 24}}
    before = deepcopy(template)
    style = effective_document_style(template, "cover_letter")
    assert style["geometry"]["font_size"] == 10.2
    assert style["typography"]["title"]["font_size"] == 19
    assert style["typography"]["header"]["alignment"] == "center"
    assert style["typography"]["heading"]["rule"] == {"color": "#B5B5B5", "width": 0.55}
    assert template == before
    style["typography"]["body"]["font_size"] = 25
    assert (
        effective_document_style(template, "cover_letter")["typography"]["body"]["font_size"]
        == 10.2
    )


@pytest.mark.parametrize("kind", ["cv", "cover_letter"])
def test_rendered_documents_use_only_grayscale_and_keep_reading_order(tmp_path, kind):
    draft = {
        "title": "Alex Example",
        "language": "en",
        "sections": [
            {
                "heading": "",
                "paragraphs": [
                    {
                        "text": "Belgrade | alex@example.com",
                        "kind": "contact",
                        "fact_ids": ["contact"],
                    },
                ],
            },
            {
                "heading": "Cover Letter" if kind == "cover_letter" else "Summary",
                "paragraphs": [
                    {"text": "Dear Hiring Team,", "fact_ids": ["name"]},
                    {
                        "text": "I test REST APIs and automate web tests with TypeScript.",
                        "fact_ids": ["api"],
                    },
                    {"text": "Best regards,", "fact_ids": ["name"]},
                    {"text": "Alex Example", "fact_ids": ["name"]},
                ],
            },
        ],
    }
    template = {
        "facts": [{"id": "api", "kind": "body"}],
        "style": {
            "typography": {
                "title": {"color": "#FF0000"},
                "header": {"color": "#00FF00"},
                "body": {"color": "#0000FF"},
                "heading": {"color": "#FF00FF", "rule": {"color": "#00FFFF", "width": 0.5}},
            },
            "paragraph_typography": {"api": {"color": "#FFFF00"}},
        },
    }
    before = deepcopy(template)
    files = render_documents(draft, template, tmp_path, kind)
    doc = Document(files[0]["path"])
    assert not doc.tables and not doc.inline_shapes
    assert "\n".join(p.text for p in doc.paragraphs) == canonical_text(draft)
    for paragraph in doc.paragraphs:
        color = paragraph.style.font.color.rgb
        assert color[0] == color[1] == color[2]
    for rule in doc.element.xpath(".//w:bottom"):
        color = rule.get(qn("w:color"))
        assert color[:2] == color[2:4] == color[4:]
    with pdfplumber.open(files[1]["path"]) as pdf:
        assert len(pdf.pages) == 1
        for page in pdf.pages:
            assert not page.images
            for character in page.chars:
                color = character["non_stroking_color"]
                assert max(color) - min(color) < 0.00001
                assert character["x0"] >= 24 and character["x1"] <= page.width - 24
            for line in page.lines:
                color = line["stroking_color"]
                assert max(color) - min(color) < 0.00001
    text = " ".join(PdfReader(files[1]["path"]).pages[0].extract_text().split())
    assert text == " ".join(canonical_text(draft).split())
    assert template == before
