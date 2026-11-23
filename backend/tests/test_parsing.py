"""File type detection and the four parsers (no Docker needed).

DOCX and PDF fixtures are generated in the test with python-docx and fpdf2, so the tests show
exactly what goes in.
"""

import io
import zipfile

import docx
import pytest
from fpdf import FPDF

from app.ingestion.filetypes import UnsupportedFileError, detect_file_type
from app.ingestion.parsers import Block, DocumentParseError, parse_document
from app.models import FileType


def make_docx() -> bytes:
    document = docx.Document()
    document.add_heading("Travel policy", level=1)
    document.add_paragraph("Book flights through the travel portal.")
    document.add_heading("Receipts", level=2)
    document.add_paragraph("Submit receipts within 30 days.", style="List Bullet")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Level"
    table.cell(0, 1).text = "Limit"
    table.cell(1, 0).text = "Manager"
    table.cell(1, 1).text = "5000 EUR"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_pdf(pages: list[str]) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for text in pages:
        pdf.add_page()
        pdf.multi_cell(0, 6, text)
    return bytes(pdf.output())


# ------------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("filename", "data", "expected"),
    [
        ("notes.md", b"# Hi", FileType.MARKDOWN),
        ("page.HTM", b"<p>hi</p>", FileType.HTML),
        ("plain.txt", "café".encode(), FileType.TEXT),
        ("bom.md", b"\xef\xbb\xbf# Hi", FileType.MARKDOWN),
        ("report.pdf", b"%PDF-1.7 ...", FileType.PDF),
    ],
)
def test_detects_supported_types(filename: str, data: bytes, expected: FileType) -> None:
    assert detect_file_type(filename, data) == expected


def test_detects_docx() -> None:
    assert detect_file_type("policy.docx", make_docx()) == FileType.DOCX


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("tool.exe", b"MZ...", "Unsupported file type"),
        ("noext", b"hello", "Unsupported file type"),
        ("empty.md", b"", "empty"),
        ("fake.pdf", b"# actually markdown", "not a valid PDF"),
        ("fake.docx", b"not a zip", "not a valid Word"),
        ("latin1.txt", "café".encode("latin-1"), "UTF-8"),
        ("binary.md", b"abc\x00def", "binary"),
    ],
)
def test_rejects_mismatched_or_unsupported_files(filename: str, data: bytes, message: str) -> None:
    with pytest.raises(UnsupportedFileError, match=message):
        detect_file_type(filename, data)


def test_rejects_zip_that_is_not_a_word_document() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(UnsupportedFileError, match="not a Word document"):
        detect_file_type("archive.docx", buffer.getvalue())


# ------------------------------------------------------------------------- parsers


def test_markdown_keeps_section_paths_lists_and_tables() -> None:
    source = """# Handbook

Intro paragraph with **bold** and a [link](https://example.test).

## Leave

### Parental leave

- Sixteen weeks paid
- Can be split

| Type | Days |
|------|------|
| Annual | 28 |

```
code stays as-is
```

## Remote work
Two days a week.
"""
    blocks = parse_document(source.encode(), FileType.MARKDOWN).blocks
    assert blocks == [
        Block("Intro paragraph with bold and a link.", ("Handbook",)),
        Block("- Sixteen weeks paid", ("Handbook", "Leave", "Parental leave")),
        Block("- Can be split", ("Handbook", "Leave", "Parental leave")),
        Block("Type | Days", ("Handbook", "Leave", "Parental leave")),
        Block("Annual | 28", ("Handbook", "Leave", "Parental leave")),
        Block("code stays as-is", ("Handbook", "Leave", "Parental leave")),
        Block("Two days a week.", ("Handbook", "Remote work")),
    ]


def test_html_drops_scripts_and_navigation() -> None:
    source = """<html><head><title>T</title><script>alert(1)</script></head><body>
      <nav>Home | About</nav>
      <h1>Security policy</h1>
      <div><p>Use a password manager.</p>
        <ul><li>Enable MFA</li></ul></div>
      <h2>Incidents</h2><p>Report within <b>one hour</b>.</p>
      <table><tr><th>Severity</th><th>Response</th></tr><tr><td>High</td><td>15 min</td></tr>
      </table>
      <footer>Copyright</footer></body></html>"""
    blocks = parse_document(source.encode(), FileType.HTML).blocks
    texts = [(b.text, b.headings) for b in blocks]
    assert texts == [
        ("Use a password manager.", ("Security policy",)),
        ("- Enable MFA", ("Security policy",)),
        ("Report within one hour .", ("Security policy", "Incidents")),
        ("Severity | Response", ("Security policy", "Incidents")),
        ("High | 15 min", ("Security policy", "Incidents")),
    ]


def test_docx_headings_lists_and_tables() -> None:
    blocks = parse_document(make_docx(), FileType.DOCX).blocks
    assert blocks == [
        Block("Book flights through the travel portal.", ("Travel policy",)),
        Block("- Submit receipts within 30 days.", ("Travel policy", "Receipts")),
        Block("Level | Limit", ("Travel policy", "Receipts")),
        Block("Manager | 5000 EUR", ("Travel policy", "Receipts")),
    ]


def test_pdf_blocks_carry_page_numbers() -> None:
    data = make_pdf(["Failover starts with paging the on-call DBA.", "Then promote the replica."])
    blocks = parse_document(data, FileType.PDF).blocks
    assert [(b.page, b.text) for b in blocks] == [
        (1, "Failover starts with paging the on-call DBA."),
        (2, "Then promote the replica."),
    ]


def test_plain_text_splits_on_blank_lines() -> None:
    blocks = parse_document(b"First line\ncontinues.\n\nSecond.", FileType.TEXT).blocks
    assert [b.text for b in blocks] == ["First line continues.", "Second."]


def test_document_without_text_fails_permanently() -> None:
    with pytest.raises(DocumentParseError, match="no text"):
        parse_document(b"<html><script>x</script></html>", FileType.HTML)


def test_corrupt_pdf_fails_permanently() -> None:
    with pytest.raises(DocumentParseError):
        parse_document(b"%PDF-1.7 this is not really a pdf", FileType.PDF)
