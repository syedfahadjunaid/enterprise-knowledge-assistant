"""Which files we accept, decided from the name AND the content.

The extension says what the uploader claims; the first bytes say what the file is. Both must
agree, so a renamed executable or a PDF called ``notes.md`` is rejected at upload time with a
clear message instead of failing later in the worker.
"""

import io
import zipfile
from pathlib import PurePosixPath

from app.models import FileType

EXTENSIONS: dict[str, FileType] = {
    ".pdf": FileType.PDF,
    ".docx": FileType.DOCX,
    ".md": FileType.MARKDOWN,
    ".markdown": FileType.MARKDOWN,
    ".html": FileType.HTML,
    ".htm": FileType.HTML,
    ".txt": FileType.TEXT,
}

# A .docx is a zip archive. Its contents are bounded so a "zip bomb" (a tiny file that expands
# to gigabytes) can't exhaust the worker's memory.
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


class UnsupportedFileError(ValueError):
    """The upload is not a file type we can ingest (user error, maps to HTTP 422)."""


def detect_file_type(filename: str, data: bytes) -> FileType:
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    file_type = EXTENSIONS.get(suffix)
    if file_type is None:
        allowed = ", ".join(sorted(EXTENSIONS))
        raise UnsupportedFileError(
            f"Unsupported file type '{suffix or filename}'. Allowed: {allowed}"
        )
    if not data:
        raise UnsupportedFileError("The file is empty")

    match file_type:
        case FileType.PDF:
            if not data.startswith(b"%PDF-"):
                raise UnsupportedFileError("The file is not a valid PDF")
        case FileType.DOCX:
            _check_docx(data)
        case FileType.MARKDOWN | FileType.HTML | FileType.TEXT:
            decode_text(data)
    return file_type


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without a byte-order mark). Anything else is rejected, not guessed."""
    if b"\x00" in data:
        raise UnsupportedFileError("The file looks binary, not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise UnsupportedFileError("Text files must be UTF-8 encoded") from e


def _check_docx(data: bytes) -> None:
    if not data.startswith(b"PK\x03\x04"):
        raise UnsupportedFileError("The file is not a valid Word document (.docx)")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as e:
        raise UnsupportedFileError("The file is not a valid Word document (.docx)") from e
    if "word/document.xml" not in names:
        raise UnsupportedFileError("The file is not a Word document (.docx)")
    if total > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise UnsupportedFileError("The Word document is too large once uncompressed")
