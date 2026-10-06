import base64
import io
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from reportlab.pdfgen import canvas

from qa_job_bot import pdf_template as pdf


def synthetic_pdf(pages=2, blank=False, columns=False, staggered=False, page_size=(595, 842)):
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=page_size)
    if blank:
        document.showPage()
    else:
        document.setFont("Helvetica-Bold", 20)
        document.drawCentredString(297.5, 798, "Alex Example")
        document.setFont("Helvetica", 9)
        document.setFillColorRGB(0.38, 0.38, 0.38)
        document.drawCentredString(297.5, 780, "QA Engineer | Remote")
        document.setFont("Helvetica-Bold", 11)
        document.setFillColorRGB(0.1, 0.1, 0.1)
        document.drawString(50, 750, "Summary")
        document.setStrokeColorRGB(0.7, 0.7, 0.7)
        document.setLineWidth(0.6)
        document.line(50, 746, 545, 746)
        document.setFont("Helvetica", 9)
        document.setFillColorRGB(0.38, 0.38, 0.38)
        if columns:
            for index in range(4):
                y = 725 - index * 14
                document.drawString(50, y, "Left column has independent experience")
                document.drawString(
                    330, y - (5 if staggered else 0), "Right column has independent experience"
                )
        else:
            document.drawString(50, 725, "Quality engineer with hands-on API testing")
            document.drawString(50, 714, "and automated regression checks for web services.")
            document.drawString(50, 696, "Separate paragraph describing collaboration.")
            document.setFont("Helvetica-Bold", 11)
            document.drawString(50, 670, "Experience")
            document.line(50, 666, 545, 666)
            document.setFont("Helvetica-Bold", 9)
            document.drawString(50, 650, "Example Company | 2022 - Present")
            document.setFont("Helvetica-Oblique", 9)
            document.drawString(50, 638, "Senior QA Engineer")
            document.setFont("Helvetica", 9)
            document.drawString(50, 620, "- Built automated API checks")
            document.drawString(56, 609, "across multiple distributed services.")
            document.drawString(50, 594, "- Reviewed release quality with the team.")
            document.setFont("Helvetica-Bold", 9)
            document.drawString(50, 569, "Previous Company | 2020 - 2022")
            document.setFont("Helvetica-Oblique", 9)
            document.drawString(50, 557, "QA Engineer")
        document.showPage()
        for _ in range(1, pages):
            document.setFont("Helvetica-Bold", 11)
            document.drawString(50, 790, "Education")
            document.line(50, 786, 545, 786)
            document.setFont("Helvetica", 9)
            document.drawString(50, 767, "Example University | 2016 - 2020")
            document.showPage()
    document.save()
    return output.getvalue()


def test_pdf_paragraphs_roles_typography_and_visual_reference():
    content = synthetic_pdf()
    result = pdf.extract_pdf_template(content)
    facts = result["facts"]
    texts = [fact["text"] for fact in facts]
    assert (
        "Quality engineer with hands-on API testing "
        "and automated regression checks for web services." in texts
    )
    assert "Separate paragraph describing collaboration." in texts
    assert "- Built automated API checks across multiple distributed services." in texts
    assert sum(fact["kind"] == "bullet" for fact in facts) == 2
    assert [fact["text"] for fact in facts if fact["kind"] == "role"] == [
        "Senior QA Engineer",
        "QA Engineer",
    ]
    assert [fact["text"] for fact in facts if fact["kind"] == "entry"] == [
        "Example Company | 2022 - Present",
        "Previous Company | 2020 - 2022",
    ]
    assert facts[0]["kind"] == "title"
    assert facts[1]["kind"] == "contact"
    assert facts[-1]["source"].startswith("page:2:block:")
    assert [fact["id"] for fact in facts] == [f"fact-{index}" for index in range(1, len(facts) + 1)]
    style = result["style"]
    assert style["source_format"] == "pdf" and style["layout_fidelity"] == "visual_reference"
    assert style["page_width"] == 595 and style["page_height"] == 842
    assert style["font_size"] == 9 and style["font_name"] == "Helvetica"
    assert style["section_order"] == ["Summary", "Experience", "Education"]
    assert style["typography"]["title"]["alignment"] == "center"
    assert style["typography"]["title"]["font_size"] == 20
    assert style["typography"]["body"]["color"] == "#616161"
    assert style["typography"]["heading"]["rule"] == {"color": "#B2B2B2", "width": 0.6}
    assert style["typography"]["italic"]["italic"] is True
    reference = result["reference"]
    assert reference["sha256"] == sha256(content).hexdigest() and reference["page_count"] == 2
    for page in reference["pages"]:
        image_bytes = base64.b64decode(page["image_base64"], validate=True)
        with Image.open(io.BytesIO(image_bytes)) as image:
            assert image.format == "PNG" and image.size == (page["width"], page["height"])
            assert max(image.size) <= pdf.MAX_REFERENCE_SIDE
        assert page["image_sha256"] == sha256(image_bytes).hexdigest()


@pytest.mark.parametrize("staggered", [False, True])
def test_independent_columns_require_user_input(staggered):
    with pytest.raises(pdf.PdfTemplateError, match="Multi-column.*needs_input"):
        pdf.extract_pdf_template(synthetic_pdf(columns=True, staggered=staggered))


@pytest.mark.parametrize(
    "options,message",
    [
        ({"blank": True}, "empty or scanned"),
        ({"page_size": (20, 842)}, "geometry"),
    ],
)
def test_unusable_pdf_is_rejected(options, message):
    with pytest.raises(pdf.PdfTemplateError, match=message):
        pdf.extract_pdf_template(synthetic_pdf(**options))


def test_pdf_limits_are_checked_before_render(monkeypatch):
    content = synthetic_pdf()
    with pytest.raises(pdf.PdfTemplateError, match="page limit"):
        pdf.extract_pdf_template(content, max_pages=1)
    with pytest.raises(pdf.PdfTemplateError, match="too much text"):
        pdf.extract_pdf_template(content, max_text=2)
    monkeypatch.setattr(pdf, "MAX_REFERENCE_PIXELS", 1)
    with pytest.raises(pdf.PdfTemplateError, match="pixel limit"):
        pdf.render_reference(content, [(595, 842)] * 2)
    monkeypatch.setattr(pdf, "MAX_REFERENCE_PIXELS", 30_000_000)
    monkeypatch.setattr(pdf, "MAX_REFERENCE_BYTES", 1)
    with pytest.raises(pdf.PdfTemplateError, match="byte limit"):
        pdf.render_reference(content, [(595, 842)] * 2)
    for dimensions in ([], [(595, 842)] * 21):
        with pytest.raises(pdf.PdfTemplateError, match="page limit"):
            pdf.render_reference(content, dimensions)
    for dimensions in ([(0, 842)], [(float("inf"), 842)], [(14_401, 842)]):
        with pytest.raises(pdf.PdfTemplateError, match="geometry"):
            pdf.render_reference(content, dimensions)
    with pytest.raises(pdf.PdfTemplateError, match="page count"):
        pdf.render_reference(content, [(595, 842)])


def text_line(text="A paragraph", **changes):
    return pdf.TextLine(
        **{
            "text": text,
            "x0": 50,
            "x1": 500,
            "top": 100,
            "bottom": 109,
            "font_name": "Helvetica",
            "font_size": 9,
            "bold": False,
            "italic": False,
            "color": "#606060",
            **changes,
        }
    )


def test_adjacent_roles_bullets_and_paragraphs_keep_independent_boundaries():
    lines = [
        text_line("First company", kind="entry", bold=True),
        text_line("Another company", kind="entry", bold=True, top=111, bottom=120),
        text_line("First role", kind="role", italic=True, top=121, bottom=130),
        text_line("Second role", kind="role", italic=True, top=131, bottom=140),
        text_line("• First bullet", kind="bullet", top=150, bottom=159),
        text_line("wrapped phrase", x0=56, top=161, bottom=170),
        text_line("• Next bullet", kind="bullet", top=172, bottom=181),
        text_line("new paragraph", x0=50, top=190, bottom=199),
        text_line("another paragraph", x0=50, top=205, bottom=214),
    ]
    assert [block["text"] for block in pdf._paragraphs(lines, 3)] == [
        "First company",
        "Another company",
        "First role",
        "Second role",
        "• First bullet wrapped phrase",
        "• Next bullet",
        "new paragraph",
        "another paragraph",
    ]
    assert all(block["source"].startswith("page:3:block:") for block in pdf._paragraphs(lines, 3))


@pytest.mark.parametrize("suffix,expected", [("-", "multi-platform"), ("\u00ad", "multiplatform")])
def test_wrap_preserves_visible_hyphen_and_removes_discretionary_hyphen(suffix, expected):
    blocks = pdf._paragraphs(
        [text_line("multi" + suffix), text_line("platform", top=111, bottom=120)], 1
    )
    assert blocks[0]["text"] == expected


def test_wrapped_separator_keeps_space_between_skill_names():
    blocks = pdf._paragraphs(
        [text_line("HTTP/REST -"), text_line("System Testing", top=111, bottom=120)], 1
    )
    assert blocks[0]["text"] == "HTTP/REST - System Testing"


def test_individual_header_weight_is_kept_separate_from_shared_role():
    blocks = pdf._paragraphs(
        [
            text_line("Contact", kind="contact"),
            text_line("QA Lead", kind="contact", bold=True, top=115, bottom=124),
        ],
        1,
    )
    assert not blocks[0]["appearance"]["bold"]
    assert blocks[1]["appearance"]["bold"]


@pytest.mark.parametrize(
    "change",
    [
        {"font_name": "Arial"},
        {"font_size": 10},
        {"bold": True},
        {"italic": True},
        {"color": "#000000"},
        {"x0": 70},
        {"top": 105},
        {"top": 120},
    ],
)
def test_visual_discontinuities_do_not_merge_paragraphs(change):
    previous = text_line()
    current = text_line("Next", top=111, bottom=120)
    assert not pdf._can_join(previous, replace(current, **change), "body")


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "#000000"),
        (0.5, "#808080"),
        ((0, 1, 1, 0), "#FF0000"),
        ((-1, 2, 0), "#00FF00"),
    ],
)
def test_pdf_color_spaces(value, expected):
    assert pdf._color(value) == expected


@pytest.mark.parametrize("value", [(1, 2), (float("nan"), 0, 0), ("bad", 0, 0)])
def test_invalid_colors_fail_closed(value):
    with pytest.raises(pdf.PdfTemplateError, match="color"):
        pdf._color(value)


def test_font_normalization_and_right_aligned_text():
    assert pdf._font_name("ABCDEF+Arial-BoldMT") == "Arial"
    assert pdf._font_name("Example-Regular") == "Example"
    lines = [text_line("Body text"), text_line("2024", x0=470, top=150, bottom=159)]
    pdf._classify(lines, SimpleNamespace(width=595, lines=[]), 9, 9, 2)
    assert lines[1].alignment == "right"
    assert "heading" not in pdf._typography(lines, [], 9)


def test_block_limit_and_text_limit_after_paragraph_join(monkeypatch):
    content = synthetic_pdf(pages=1)
    monkeypatch.setattr(
        pdf,
        "_paragraphs",
        lambda lines, page: [{"text": "a", "kind": "body", "source": "page:1:block:1"}] * 501,
    )
    with pytest.raises(pdf.PdfTemplateError, match="too much text"):
        pdf.extract_pdf_template(content)
    monkeypatch.setattr(
        pdf,
        "_paragraphs",
        lambda lines, page: [{"text": "a" * 100_001, "kind": "body", "source": "page:1:block:1"}],
    )
    with pytest.raises(pdf.PdfTemplateError, match="too much text"):
        pdf.extract_pdf_template(content)


def test_title_only_pdf_uses_available_typography():
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.setFont("Helvetica-Bold", 12)
    document.drawString(72, 720, "Example title")
    document.save()
    result = pdf.extract_pdf_template(output.getvalue())
    assert result["style"]["font_size"] == 12
    assert result["facts"][0]["kind"] == "entry"


def winansi_bullet_pdf():
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.setFont("Helvetica", 10)
    document.drawString(50, 720, "• Implemented automated checks")
    document.drawString(56, 708, "for API services.")
    document.drawString(50, 690, "• Reviewed test coverage.")
    document.save()
    return output.getvalue()


def test_winansi_bullet_is_recovered_from_explicit_font_encoding():
    parsed = pdf.extract_pdf_template(winansi_bullet_pdf())
    assert [fact["text"] for fact in parsed["facts"]] == [
        "• Implemented automated checks for API services.",
        "• Reviewed test coverage.",
    ]
    assert all(fact["kind"] == "bullet" for fact in parsed["facts"])


def test_unmapped_glyph_is_not_invented_without_encoding_evidence():
    reader = PdfReader(io.BytesIO(winansi_bullet_pdf()))
    for value in reader.pages[0]["/Resources"]["/Font"].values():
        value.get_object()[NameObject("/Encoding")] = NameObject("/StandardEncoding")
    output = io.BytesIO()
    PdfWriter(clone_from=reader).write(output)
    with pytest.raises(pdf.PdfTemplateError, match="unmapped characters.*needs_input"):
        pdf.extract_pdf_template(output.getvalue())


def test_sparse_template_infers_bounded_margins_for_generation():
    from qa_job_bot.document_files import _geometry

    parsed = pdf.extract_pdf_template(winansi_bullet_pdf())
    geometry = _geometry(parsed["style"])
    assert 24 <= geometry["margin_left"] <= 110
    assert geometry["margin_right"] == 110
    assert geometry["margin_bottom"] == 110


def test_sparse_template_preserves_actual_baselines_and_block_gaps(tmp_path):
    import pdfplumber

    from qa_job_bot.document_files import render_documents

    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(595, 842))
    for text, font, size, y in (
        ("Alex Example", "Helvetica-Bold", 22, 782),
        ("QA Engineer", "Helvetica", 11, 756),
        ("Experience", "Helvetica-Bold", 14, 712),
        ("Tested REST APIs and web applications.", "Helvetica", 11, 690),
    ):
        document.setFont(font, size)
        document.drawString(52, y, text)
    document.save()
    template = pdf.extract_pdf_template(output.getvalue())
    draft = {
        "title": "Alex Example",
        "language": "en",
        "sections": [
            {
                "heading": "",
                "paragraphs": [{"text": "QA Engineer", "kind": "role", "fact_ids": ["fact-2"]}],
            },
            {
                "heading": "Experience",
                "paragraphs": [
                    {"text": "Tested REST APIs and web applications.", "fact_ids": ["fact-4"]}
                ],
            },
        ],
    }
    render_documents(draft, template, tmp_path)
    with pdfplumber.open(io.BytesIO(output.getvalue())) as original:
        expected = pdf._page_lines(original.pages[0])
    with pdfplumber.open(tmp_path / "document.pdf") as rendered:
        actual = pdf._page_lines(rendered.pages[0])
    assert [line.text for line in actual] == [line.text for line in expected]
    assert [line.baseline for line in actual] == pytest.approx([line.baseline for line in expected])
    assert [line.x0 for line in actual] == pytest.approx([line.x0 for line in expected])
    assert [line.top for line in actual] == pytest.approx([line.top for line in expected])
    assert template["style"]["margin_top"] == 38
    assert template["style"]["typography"]["heading"]["space_before"] == 27.25
    assert template["style"]["typography"]["heading"]["space_after"] == 7.5
