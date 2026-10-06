import base64
import io
import json
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest
from docx import Document
from docx.shared import Pt
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject
from reportlab.pdfgen import canvas

from qa_job_bot import document_files as files


def docx_bytes(text="Alex tests APIs", size=None):
    output = io.BytesIO()
    document = Document()
    document.add_paragraph(text)
    if size:
        document.styles["Normal"].font.size = Pt(size)
        document.styles["Normal"].font.name = "Arial"
    document.save(output)
    return output.getvalue()


def pdf_bytes(text="Alex tests APIs", pages=1):
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    for _ in range(pages):
        if text:
            pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def with_entry(content, name, payload):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, "w") as target:
        for item in source.infolist():
            if item.filename != name:
                target.writestr(item, source.read(item))
        target.writestr(name, payload)
    return output.getvalue()


def document():
    return {
        "title": "Alex Example",
        "language": "en",
        "sections": [
            {
                "heading": "Experience",
                "paragraphs": [{"text": "QA Engineer\nREST API testing", "fact_ids": ["f1"]}],
            },
            {"heading": "", "paragraphs": [{"text": "Postman & Jira", "fact_ids": ["f1"]}]},
        ],
    }


@pytest.mark.parametrize("size", [None, 12])
def test_docx_text_and_style(size):
    result = files._parse_template(docx_bytes(size=size), "CV.DOCX")
    assert result["facts"] == [{"id": "fact-1", "text": "Alex tests APIs", "source": "paragraph:1"}]
    assert result["style"]["font_size"] == (size or 11)
    assert result["style"]["layout_fidelity"] == "typography_and_page_geometry"


def test_pdf_text_and_process_isolation():
    result = files.parse_template(pdf_bytes(), "CV.pdf")
    assert result["extracted_text"] == "Alex tests APIs"
    assert result["style"]["layout_fidelity"] == "visual_reference"
    assert result["facts"][0]["source"] == "page:1:block:1"
    assert result["facts"][0]["kind"] == "body"
    assert result["reference"]["page_count"] == 1
    assert base64.b64decode(result["reference"]["pages"][0]["image_base64"]).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    assert files.parse_template(docx_bytes(), "CV.docx")["facts"][0]["id"] == "fact-1"


def test_isolated_pdf_parser_returns_complete_visual_payload():
    output = io.BytesIO()
    document = canvas.Canvas(output)
    for page in range(2):
        document.setFont("Helvetica", 9)
        for line in range(35):
            document.drawString(
                50, 790 - line * 16, f"Page {page + 1}, example QA evidence paragraph {line + 1}."
            )
        document.showPage()
    document.save()
    parsed = files.parse_template(output.getvalue(), "example.pdf")
    assert len(parsed["facts"]) == 70
    assert len(json.dumps(parsed)) > 65_536
    assert parsed["reference"]["page_count"] == 2
    assert parsed["facts"][-1]["source"] == "page:2:block:35"
    assert parsed["facts"][-1]["text"].endswith("paragraph 35.")


def test_active_pdf_rejected_before_geometry_and_rendering(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Active PDF reached geometry/rendering")

    monkeypatch.setattr(files, "extract_pdf_template", unexpected)
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes())))
    writer.add_js("app.alert('unsafe')")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(files.DocumentFileError, match="Active PDF"):
        files._parse_template(output.getvalue(), "CV.pdf")


@pytest.mark.parametrize(
    "dimensions,allowed",
    [
        ([(10, 10)], True),
        ([(-1, 10)], False),
        ([(10000, 10000)], False),
        ([(5000, 4000), (5000, 4000)], False),
    ],
)
def test_pdf_image_resource_limits_apply_before_native_decoder(monkeypatch, dimensions, allowed):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes())))
    resources = DictionaryObject()
    for index, (width, height) in enumerate(dimensions):
        resource = DictionaryObject(
            {
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(width),
                NameObject("/Height"): NumberObject(height),
            }
        )
        resources[NameObject(f"/Image{index}")] = writer._add_object(resource)
    writer.pages[0][NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): resources}
    )
    output = io.BytesIO()
    writer.write(output)
    calls = []
    monkeypatch.setattr(
        files, "extract_pdf_template", lambda *args, **kwargs: calls.append(True) or {"safe": True}
    )
    if allowed:
        assert files._pdf(output.getvalue()) == {"safe": True}
        assert calls == [True]
    else:
        with pytest.raises(files.DocumentFileError, match="image"):
            files._pdf(output.getvalue())
        assert calls == []


@pytest.mark.parametrize(
    "content,name",
    [
        (b"", "cv.docx"),
        (b"PKbad", "cv.docx"),
        (b"%PDF-invalid", "cv.pdf"),
        (b"hello", "cv.txt"),
        (b"%PDF", "cv.docx"),
        (b"x" * (files.MAX_UPLOAD_BYTES + 1), "cv.pdf"),
    ],
)
def test_bad_file(content, name):
    with pytest.raises(files.DocumentFileError):
        files._parse_template(content, name)


@pytest.mark.parametrize(
    "name,payload",
    [
        ("../secret.xml", b"<root/>"),
        ("/secret.xml", b"<root/>"),
        ("word\\secret.xml", b"<root/>"),
        ("word/vbaProject.bin", b"bad"),
        ("word/embeddings/item.bin", b"bad"),
        ("word/document.xml", b'<!DOCTYPE test [<!ENTITY x "a">]><root/>'),
        ("word/document.xml", b"<root><vanish/></root>"),
        (
            "word/_rels/evil.rels",
            b'<Relationships><Relationship TargetMode="External" Type="image" Target="http://tracking.example"/></Relationships>',
        ),
        (
            "word/_rels/evil.rels",
            b'<Relationships><Relationship TargetMode="External" Type="x/hyperlink" Target="file:///secret"/></Relationships>',
        ),
    ],
)
def test_docx_attacks(name, payload):
    with pytest.raises(files.DocumentFileError):
        files._parse_template(with_entry(docx_bytes(), name, payload), "cv.docx")


def test_hyperlink_is_inert_and_technical_commands_are_data():
    payload = b'<Relationships><Relationship TargetMode="External" Type="x/hyperlink" Target="https://example.com"/></Relationships>'
    result = files._parse_template(
        with_entry(docx_bytes("Test command: pytest -q"), "word/_rels/evil.rels", payload),
        "cv.docx",
    )
    assert result["extracted_text"] == "Test command: pytest -q"


def test_archive_limits():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(251):
            archive.writestr(f"{index}.xml", b"<root/>")
    with pytest.raises(files.DocumentFileError, match="limits"):
        files._parse_template(output.getvalue(), "cv.docx")
    content = with_entry(docx_bytes(), "word/big.bin", b"a" * 10_000_001)
    with pytest.raises(files.DocumentFileError, match="oversized"):
        files._parse_template(content, "cv.docx")


def test_pdf_active_encrypted_empty_and_page_limits():
    source = pdf_bytes()
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(source)))
    writer.add_js("app.alert('unsafe')")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(files.DocumentFileError, match="Active PDF"):
        files._parse_template(output.getvalue(), "cv.pdf")
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(source)))
    writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(files.DocumentFileError, match="Encrypted"):
        files._parse_template(output.getvalue(), "cv.pdf")
    with pytest.raises(files.DocumentFileError, match="page limit"):
        files._parse_template(pdf_bytes(pages=files.MAX_PAGES + 1), "cv.pdf")
    with pytest.raises(files.DocumentFileError, match="empty"):
        files._parse_template(pdf_bytes(text=""), "cv.pdf")
    assert files._parse_template(pdf_bytes(pages=files.MAX_PAGES), "cv.pdf")["facts"]


@pytest.mark.parametrize("text", ["", "a" * (files.MAX_TEXT + 1), "QA\n" * 501])
def test_empty_or_excessive_text(text):
    with pytest.raises(files.DocumentFileError):
        files._parse_template(docx_bytes(text), "cv.docx")


def test_render_pdf_docx_agree_and_keep_geometry(tmp_path):
    result = files.render_documents(document(), {"style": {"font_size": 12}}, tmp_path)
    assert [item["format"] for item in result] == ["docx", "pdf"]
    assert all(item["path"].stat().st_mode & 0o777 == 0o600 for item in result)
    assert Document(result[0]["path"]).styles["Normal"].font.size.pt == 12
    assert not Document(result[0]["path"]).styles["Title"].element.xpath(".//w:pBdr")
    assert "Postman & Jira" in PdfReader(result[1]["path"]).pages[0].extract_text()


def test_render_rejects_invalid_geometry_fonts_and_text_mismatch(tmp_path, monkeypatch):
    with pytest.raises(files.DocumentFileError, match="geometry"):
        files.render_documents(document(), {"style": {"margin_left": 1000}}, tmp_path)
    monkeypatch.setattr(files, "FONT_CANDIDATES", ["/nonexistent/font.ttf"])
    with pytest.raises(files.DocumentFileError, match="font"):
        files.render_documents(document(), {}, tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(files, "canonical_text", lambda _: "different")
    with pytest.raises(files.DocumentFileError, match="differs"):
        files.render_documents(document(), {}, tmp_path)


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.TimeoutExpired("parser", 15),
        OSError("missing"),
        SimpleNamespace(stdout=b"bad", returncode=0),
        SimpleNamespace(stdout=b'{"error":"Rejected"}', returncode=0),
        SimpleNamespace(stdout=b"{}", returncode=1),
    ],
)
def test_parser_process_failure(monkeypatch, outcome):
    def run(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(files.subprocess, "run", run)
    with pytest.raises(files.DocumentFileError):
        files.parse_template(b"anything", "CV.pdf")
    with pytest.raises(files.DocumentFileError):
        files.parse_template(b"", "CV.pdf")


def test_cli_parser_main(monkeypatch):
    monkeypatch.setattr(files.resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(docx_bytes())))
    monkeypatch.setattr(sys, "argv", ["parser", ".docx"])
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    files.main()
    assert json.loads(output.getvalue())["extracted_text"] == "Alex tests APIs"
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"invalid")))
    output.seek(0)
    output.truncate()
    files.main()
    assert "error" in json.loads(output.getvalue())
