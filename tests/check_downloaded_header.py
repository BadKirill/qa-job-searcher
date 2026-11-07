import sys

import pdfplumber
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH


def check_header(pdf_path, docx_path):
    document = Document(docx_path)
    heading = next(
        index for index, paragraph in enumerate(document.paragraphs)
        if paragraph.text == "Experience"
    )
    assert heading >= 3
    for paragraph in document.paragraphs[:heading]:
        assert paragraph.style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert (
        document.paragraphs[heading + 1].style.paragraph_format.alignment
        == WD_ALIGN_PARAGRAPH.LEFT
    )
    section = document.sections[0]
    center = (section.page_width.pt + section.left_margin.pt - section.right_margin.pt) / 2
    with pdfplumber.open(pdf_path) as pdf:
        lines = pdf.pages[0].extract_text_lines()
        heading_line = next(i for i, line in enumerate(lines) if line["text"] == "Experience")
        assert heading_line >= heading
        for line in lines[:heading_line]:
            assert abs((line["x0"] + line["x1"]) / 2 - center) < 0.1
        assert abs(lines[heading_line + 1]["x0"] - section.left_margin.pt) < 0.1


if __name__ == "__main__":
    check_header(*sys.argv[1:])
