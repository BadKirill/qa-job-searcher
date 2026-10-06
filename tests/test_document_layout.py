from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from pypdf import PdfReader

from qa_job_bot import document_layout as layout
from qa_job_bot.document_files import DocumentFileError, _geometry, parse_template, render_documents


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
                    {"text": "Testing", "fact_ids": ["f1", "f2"]},
                ],
            }
        ],
    }
    template = {
        "style": {
            "paragraph_typography": {
                "f2": {
                    "bold": True,
                    "alignment": "center",
                    "color": "red" if invalid else "#606060",
                }
            }
        }
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
        assert doc.paragraphs[3].style.name == "Normal"
