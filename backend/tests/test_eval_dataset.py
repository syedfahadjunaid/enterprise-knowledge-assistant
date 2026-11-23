"""The retrieval eval dataset stays consistent with the demo corpus (no Docker needed)."""

import json
from pathlib import Path

from app.ingestion.chunking import chunk_blocks
from app.ingestion.filetypes import detect_file_type
from app.ingestion.parsers import parse_document

REPO = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((REPO / "demo-docs" / "manifest.json").read_text("utf-8"))
DATASET = json.loads(
    (REPO / "backend" / "evals" / "retrieval" / "datasets" / "demo.json").read_text("utf-8")
)


def _section_labels(file: str) -> list[str]:
    data = (REPO / "demo-docs" / file).read_bytes()
    chunks = chunk_blocks(parse_document(data, detect_file_type(file, data)).blocks)
    return [f"{c.heading or ''} {f'page {c.page}' if c.page else ''}".lower() for c in chunks]


def test_question_ids_and_texts_are_unique() -> None:
    questions = DATASET["questions"]
    assert len({q["id"] for q in questions}) == len(questions)
    assert len({q["question"].lower() for q in questions}) == len(questions)


def test_every_expected_section_exists_in_its_document() -> None:
    files = {d["file"] for d in MANIFEST["documents"]}
    for q in DATASET["questions"]:
        if q["expected"] is None:
            assert "section" not in q, q["id"]
            continue
        assert q["expected"] in files, q["id"]
        labels = _section_labels(q["expected"])
        assert any(q["section"].lower() in label for label in labels), q["id"]


def test_every_document_has_questions_and_there_are_unanswerable_ones() -> None:
    covered = {q["expected"] for q in DATASET["questions"]}
    assert {d["file"] for d in MANIFEST["documents"]} <= covered
    assert sum(q["expected"] is None for q in DATASET["questions"]) >= 5
