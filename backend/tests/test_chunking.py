"""Chunking rules (no Docker needed)."""

import pytest

from app.ingestion.chunking import ChunkDraft, chunk_blocks, embedding_text
from app.ingestion.parsers import Block


def test_small_section_becomes_one_chunk_with_its_heading() -> None:
    blocks = [Block("First.", ("Leave", "Parental")), Block("Second.", ("Leave", "Parental"))]
    assert chunk_blocks(blocks) == [ChunkDraft(0, "First.\n\nSecond.", "Leave > Parental", None)]


def test_chunks_never_mix_sections_or_pages() -> None:
    blocks = [
        Block("A1.", ("A",)),
        Block("B1.", ("B",)),
        Block("P1.", page=1),
        Block("P2.", page=2),
    ]
    chunks = chunk_blocks(blocks)
    assert [(c.text, c.heading, c.page) for c in chunks] == [
        ("A1.", "A", None),
        ("B1.", "B", None),
        ("P1.", None, 1),
        ("P2.", None, 2),
    ]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]


def test_long_sections_are_split_with_overlap_and_respect_the_limit() -> None:
    sentences = [f"Sentence number {i} explains rule {i} in detail." for i in range(60)]
    blocks = [Block(" ".join(sentences[i : i + 5]), ("Rules",)) for i in range(0, 60, 5)]
    chunks = chunk_blocks(blocks, max_chars=400, overlap_chars=80)

    assert len(chunks) > 3
    assert all(len(c.text) <= 400 for c in chunks)
    # Every sentence survives somewhere.
    joined = " ".join(c.text for c in chunks)
    assert all(s in joined for s in sentences)
    # Consecutive chunks share text (the overlap).
    for previous, current in zip(chunks, chunks[1:], strict=False):
        last_sentence = previous.text.split(". ")[-1].rstrip(".")
        assert last_sentence in current.text


def test_a_giant_sentence_is_split_between_words() -> None:
    words = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_blocks([Block(words)], max_chars=200, overlap_chars=0)
    assert all(len(c.text) <= 200 for c in chunks)
    assert " ".join(c.text for c in chunks).split() == words.split()


def test_overlap_must_be_smaller_than_half_a_chunk() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunk_blocks([Block("x")], max_chars=100, overlap_chars=60)


def test_embedding_text_adds_document_and_section_context() -> None:
    chunk = ChunkDraft(0, "Sixteen weeks.", "Leave > Parental", None)
    assert embedding_text("Handbook", chunk) == "Handbook > Leave > Parental\n\nSixteen weeks."
