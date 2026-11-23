"""File bytes -> a flat list of text blocks, each tagged with where it sits in the document.

Every format ends up in the same shape, ``ParsedDocument(blocks=[Block(...), ...])``, so the
chunker doesn't care whether text came from a PDF page or a Markdown section:

* ``text``      one paragraph, list item, table row or code block, as plain text
* ``headings``  the section it belongs to, e.g. ("Expenses", "Travel")
* ``page``      1-based page number (PDFs only), so answers can cite "page 3"

Parsing problems that retrying won't fix (corrupt file, no text at all) raise
``DocumentParseError``; the worker marks the document FAILED with that message.
"""

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass

from app.ingestion.filetypes import UnsupportedFileError, decode_text
from app.models import FileType


@dataclass(frozen=True)
class Block:
    text: str
    headings: tuple[str, ...] = ()
    page: int | None = None


@dataclass(frozen=True)
class ParsedDocument:
    blocks: list[Block]


class DocumentParseError(Exception):
    """The document can't be turned into text. Permanent: the worker won't retry it."""


def parse_document(data: bytes, file_type: FileType) -> ParsedDocument:
    try:
        match file_type:
            case FileType.MARKDOWN:
                blocks = list(_parse_markdown(decode_text(data)))
            case FileType.HTML:
                blocks = list(_parse_html(decode_text(data)))
            case FileType.TEXT:
                blocks = [Block(p) for p in _paragraphs(decode_text(data))]
            case FileType.DOCX:
                blocks = list(_parse_docx(data))
            case FileType.PDF:
                blocks = list(_parse_pdf(data))
    except (DocumentParseError, UnsupportedFileError) as e:
        raise DocumentParseError(str(e)) from e
    except Exception as e:  # noqa: BLE001 - parser libraries raise many types for corrupt input
        raise DocumentParseError(f"Could not read the {file_type} file: {e}") from e

    blocks = [b for b in blocks if b.text.strip()]
    if not blocks:
        hint = " (a scanned PDF? OCR is not supported yet)" if file_type == FileType.PDF else ""
        raise DocumentParseError(f"The document contains no text{hint}")
    return ParsedDocument(blocks)


# --------------------------------------------------------------------------------- helpers


def _clean(text: str) -> str:
    """Collapse runs of whitespace into single spaces."""
    return " ".join(text.split())


def _paragraphs(text: str) -> Iterator[str]:
    """Split plain text on blank lines; join the lines inside a paragraph."""
    for raw in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        paragraph = _clean(raw)
        if paragraph:
            yield paragraph


class _HeadingStack:
    """Tracks the current section path as headings of different levels go by."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        if title:
            self._stack.append((level, title))

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(title for _, title in self._stack)


# --------------------------------------------------------------------------------- Markdown


def _parse_markdown(text: str) -> Iterator[Block]:
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    headings = _HeadingStack()
    list_depth = 0
    row: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        match token.type:
            case "heading_open":
                level = int(token.tag[1])  # "h2" -> 2
                headings.push(level, _inline_text(tokens[i + 1]))
                i += 2  # skip the inline token; heading_close is skipped below
            case "bullet_list_open" | "ordered_list_open":
                list_depth += 1
            case "bullet_list_close" | "ordered_list_close":
                list_depth -= 1
            case "inline" if tokens[i - 1].type == "paragraph_open":
                content = _inline_text(token)
                prefix = "- " if list_depth else ""
                yield Block(prefix + content, headings.path)
            case "fence" | "code_block":
                yield Block(token.content.rstrip(), headings.path)
            case "tr_open":
                row = []
            case "inline" if tokens[i - 1].type in ("th_open", "td_open"):
                row.append(_inline_text(token))
            case "tr_close":
                yield Block(" | ".join(row), headings.path)
        i += 1


def _inline_text(token: object) -> str:
    """Plain text of a markdown-it inline token: drops **emphasis** marks, keeps link text."""
    parts: list[str] = []
    for child in getattr(token, "children", None) or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "image":
            parts.append(child.content)  # alt text
    return _clean("".join(parts))


# --------------------------------------------------------------------------------- HTML

_HTML_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form"}
_HTML_BLOCKS = {"p", "li", "blockquote", "dt", "dd", "figcaption", "caption", "address"}
_HTML_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _parse_html(text: str) -> Iterator[Block]:
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag

    soup = BeautifulSoup(text, "html.parser")  # pure Python parser: no native dependencies
    root = soup.body or soup
    headings = _HeadingStack()

    def walk(node: Tag) -> Iterator[Block]:
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                loose = _clean(str(child))
                if loose:
                    yield Block(loose, headings.path)
                continue
            if not isinstance(child, Tag) or child.name in _HTML_SKIP:
                continue
            if child.name in _HTML_HEADINGS:
                headings.push(int(child.name[1]), _clean(child.get_text(" ")))
            elif child.name in _HTML_BLOCKS:
                prefix = "- " if child.name == "li" else ""
                yield Block(prefix + _clean(child.get_text(" ")), headings.path)
            elif child.name == "pre":
                yield Block(child.get_text().rstrip(), headings.path)
            elif child.name == "table":
                for tr in child.find_all("tr"):
                    cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
                    yield Block(" | ".join(cells), headings.path)
            else:
                yield from walk(child)  # div, section, article, main, ul, ...

    yield from walk(root)


# --------------------------------------------------------------------------------- DOCX


def _parse_docx(data: bytes) -> Iterator[Block]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    headings = _HeadingStack()
    # iter_inner_content() yields paragraphs and tables in document order.
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = _clean(item.text)
            style = item.style.name if item.style is not None else ""
            level = _docx_heading_level(style)
            if level is not None:
                headings.push(level, text)
            elif text:
                prefix = "- " if style.startswith("List") else ""
                yield Block(prefix + text, headings.path)
        elif isinstance(item, Table):
            for row in item.rows:
                cells = [_clean(cell.text) for cell in row.cells]
                yield Block(" | ".join(cells), headings.path)


def _docx_heading_level(style: str) -> int | None:
    if style == "Title":
        return 1
    match = re.fullmatch(r"Heading (\d)", style)
    return int(match.group(1)) + 1 if match else None  # "Title" sits above "Heading 1"


# --------------------------------------------------------------------------------- PDF


def _parse_pdf(data: bytes) -> Iterator[Block]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise DocumentParseError("The PDF is password-protected")
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = re.sub(r"-\n(?=[a-z])", "", text)  # re-join words hyphenated across lines
        for paragraph in _paragraphs(text):
            yield Block(paragraph, page=number)
