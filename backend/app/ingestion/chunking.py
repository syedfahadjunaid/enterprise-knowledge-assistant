"""Blocks -> chunks: the passages that get embedded, searched and cited.

Rules, in order of importance:

1. A chunk never mixes sections (or PDF pages). Its heading and page are then exact, which is
   what makes a citation like "Expense policy > Travel, page 2" trustworthy.
2. Chunks are at most ``max_chars`` long (~300 tokens at 1200 chars; bge-small reads at most
   512 tokens and ignores the rest). Paragraphs are kept whole when they fit; longer ones are
   split between sentences, and a sentence longer than a whole chunk between words.
3. Consecutive chunks of one section overlap by up to ``overlap_chars``, so a sentence that
   needs its neighbour for context is still findable.

``embedding_text`` adds the document title and section path in front of the chunk text before
embedding: "Leave policy > Parental leave" tells the model what "16 weeks" is about. The
stored and displayed text stays the plain chunk text.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import groupby

from app.ingestion.parsers import Block

HEADING_SEPARATOR = " > "
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    text: str
    heading: str | None
    page: int | None


def chunk_blocks(
    blocks: Sequence[Block], max_chars: int = 1200, overlap_chars: int = 150
) -> list[ChunkDraft]:
    if overlap_chars >= max_chars // 2:
        raise ValueError("overlap_chars must be less than half of max_chars")
    drafts: list[ChunkDraft] = []
    for (headings, page), section in groupby(blocks, key=lambda b: (b.headings, b.page)):
        heading = HEADING_SEPARATOR.join(headings) or None
        for text in _pack([b.text for b in section], max_chars, overlap_chars):
            drafts.append(ChunkDraft(len(drafts), text, heading, page))
    return drafts


def embedding_text(document_title: str, chunk: ChunkDraft) -> str:
    context = HEADING_SEPARATOR.join(p for p in (document_title, chunk.heading) if p)
    return f"{context}\n\n{chunk.text}" if context else chunk.text


def _pack(paragraphs: list[str], max_chars: int, overlap_chars: int) -> Iterator[str]:
    """Greedy packing of paragraphs (split further when too long) into chunks of a section."""
    current = ""
    for unit, separator in _units(paragraphs, max_chars):
        candidate = f"{current}{separator}{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue
        yield current
        overlap = _tail(current, overlap_chars)
        with_overlap = f"{overlap} {unit}" if overlap else unit
        current = with_overlap if len(with_overlap) <= max_chars else unit
    if current:
        yield current


def _units(paragraphs: list[str], max_chars: int) -> Iterator[tuple[str, str]]:
    """(text, separator-before-it) pieces that each fit in one chunk."""
    for paragraph in paragraphs:
        pieces = [paragraph] if len(paragraph) <= max_chars else _split_long(paragraph, max_chars)
        for i, piece in enumerate(pieces):
            yield piece, "\n\n" if i == 0 else " "


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        words, line = sentence.split(), ""
        for word in words:
            if line and len(line) + 1 + len(word) > max_chars:
                pieces.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word[:max_chars]
        if line:
            pieces.append(line)
    # Re-merge short sentences so pieces are as large as allowed.
    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) + 1 + len(piece) <= max_chars:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def _tail(text: str, overlap_chars: int) -> str:
    """The last whole sentences (or words) of ``text`` that fit in ``overlap_chars``."""
    if overlap_chars <= 0:
        return ""
    sentences = _SENTENCE_END.split(text)
    tail = ""
    for sentence in reversed(sentences):
        candidate = f"{sentence} {tail}".strip()
        if len(candidate) > overlap_chars:
            break
        tail = candidate
    if tail:
        return tail
    # No sentence fits: fall back to whole words from the end.
    words = text.split()
    tail_words: list[str] = []
    for word in reversed(words):
        if len(" ".join([word, *tail_words])) > overlap_chars:
            break
        tail_words.insert(0, word)
    return " ".join(tail_words)
