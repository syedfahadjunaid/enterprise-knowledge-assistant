"""Retrieval evaluation: does the right passage come first, and does "not found" work?

Runs the real pipeline (parsers -> chunker -> embedder) over the demo corpus, then ranks
chunks for every question in a dataset by cosine similarity: the same ranking the pgvector
query computes (``ORDER BY embedding <=> query``), here in numpy so no database is needed.
Permissions are not involved here; tests/test_permission_eval.py checks those against Postgres.

Metrics (answerable questions):
  hit@1     the top chunk comes from the right document
  section@1 ... and from the right section (or PDF page)
  hit@3     the right document is among the top 3 documents
  MRR       mean reciprocal rank of the right document
With the "not found" cut-off (min score):
  accuracy  answerable: right document AND score >= cut-off; unanswerable: top score < cut-off
Every number has a 95% bootstrap confidence interval: with ~50 questions they are wide.

Usage (from backend/):
  python -m evals.retrieval.run                              # bge-small, default settings
  python -m evals.retrieval.run --model hashing-v1           # word-matching baseline
  python -m evals.retrieval.run --compare heading-context    # paired comparison off/on
  python -m evals.retrieval.run --sweep                      # accuracy at other cut-offs
  python -m evals.retrieval.run --show-misses --fail-under 0.8
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.ingestion.chunking import ChunkDraft, chunk_blocks, embedding_text
from app.ingestion.filetypes import detect_file_type
from app.ingestion.parsers import parse_document
from app.retrieval.embeddings import Embedder, build_embedder, default_min_score

REPO = Path(__file__).resolve().parents[3]
DEMO_DOCS = REPO / "demo-docs"
DATASETS = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class IndexedChunk:
    file: str
    draft: ChunkDraft

    @property
    def section_label(self) -> str:
        page = f"page {self.draft.page}" if self.draft.page else ""
        return f"{self.draft.heading or ''} {page}".strip()


@dataclass(frozen=True)
class Outcome:
    qid: str
    question: str
    expected: str | None
    top_file: str
    top_section: str
    top_score: float
    doc_rank: int | None  # 1-based rank of the expected document among distinct documents
    section_ok: bool

    def correct_at(self, cut_off: float) -> bool:
        if self.expected is None:
            return self.top_score < cut_off
        return self.doc_rank == 1 and self.top_score >= cut_off


def load_corpus(
    embedder: Embedder, *, max_chars: int, overlap: int, heading_context: bool
) -> tuple[list[IndexedChunk], np.ndarray]:
    manifest = json.loads((DEMO_DOCS / "manifest.json").read_text(encoding="utf-8"))
    chunks: list[IndexedChunk] = []
    texts: list[str] = []
    for doc in manifest["documents"]:
        data = (DEMO_DOCS / doc["file"]).read_bytes()
        parsed = parse_document(data, detect_file_type(doc["file"], data))
        for draft in chunk_blocks(parsed.blocks, max_chars=max_chars, overlap_chars=overlap):
            chunks.append(IndexedChunk(doc["file"], draft))
            texts.append(embedding_text(doc["title"], draft) if heading_context else draft.text)
    matrix = np.vstack(embedder.embed_documents(texts))
    return chunks, matrix


def evaluate(
    embedder: Embedder, chunks: list[IndexedChunk], matrix: np.ndarray, questions: list[dict]
) -> list[Outcome]:
    outcomes = []
    for q in questions:
        scores = matrix @ embedder.embed_query(q["question"])  # vectors are unit length
        order = np.argsort(-scores)
        top = chunks[order[0]]
        ranked_docs = list(dict.fromkeys(chunks[i].file for i in order))
        expected = q["expected"]
        doc_rank = ranked_docs.index(expected) + 1 if expected in ranked_docs else None
        section_ok = (
            expected is not None
            and top.file == expected
            and q["section"].lower() in top.section_label.lower()
        )
        outcomes.append(
            Outcome(
                q["id"],
                q["question"],
                expected,
                top.file,
                top.section_label,
                float(scores[order[0]]),
                doc_rank,
                section_ok,
            )
        )
    return outcomes


def bootstrap_ci(values: np.ndarray, n: int = 4000, seed: int = 7) -> tuple[float, float]:
    if len(values) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(n, len(values)), replace=True).mean(axis=1)
    return float(np.percentile(samples, 2.5)), float(np.percentile(samples, 97.5))


def metrics(outcomes: list[Outcome], cut_off: float) -> dict[str, tuple[float, float, float]]:
    answerable = [o for o in outcomes if o.expected is not None]
    unanswerable = [o for o in outcomes if o.expected is None]

    def stat(values: list[float]) -> tuple[float, float, float]:
        arr = np.asarray(values, dtype=float)
        low, high = bootstrap_ci(arr)
        return float(arr.mean()) if len(arr) else float("nan"), low, high

    return {
        "hit@1": stat([o.doc_rank == 1 for o in answerable]),
        "section@1": stat([o.section_ok for o in answerable]),
        "hit@3": stat([o.doc_rank is not None and o.doc_rank <= 3 for o in answerable]),
        "MRR": stat([1 / o.doc_rank if o.doc_rank else 0.0 for o in answerable]),
        "answered@cut-off": stat([o.correct_at(cut_off) for o in answerable]),
        "not-found@cut-off": stat([o.correct_at(cut_off) for o in unanswerable]),
        "accuracy@cut-off": stat([o.correct_at(cut_off) for o in outcomes]),
    }


def print_metrics(title: str, result: dict, n_answerable: int, n_unanswerable: int) -> None:
    print(f"\n{title}  ({n_answerable} answerable, {n_unanswerable} unanswerable questions)")
    for name, (mean, low, high) in result.items():
        print(f"  {name:18} {mean:6.1%}   95% CI [{low:5.1%}, {high:5.1%}]")


def paired_difference(a: list[Outcome], b: list[Outcome], cut_off: float) -> None:
    """B minus A on the same questions, with a paired bootstrap CI."""
    for label, pick in (
        ("hit@1", lambda o: o.doc_rank == 1),
        ("accuracy@cut-off", lambda o: o.correct_at(cut_off)),
    ):
        pairs = [(pick(x), pick(y)) for x, y in zip(a, b, strict=True)]
        if label == "hit@1":
            pairs = [p for p, o in zip(pairs, a, strict=True) if o.expected is not None]
        diff = np.asarray([float(y) - float(x) for x, y in pairs])
        low, high = bootstrap_ci(diff)
        verdict = "better" if low > 0 else "worse" if high < 0 else "no clear difference"
        print(f"  {label:18} {diff.mean():+6.1%}   95% CI [{low:+5.1%}, {high:+5.1%}]  {verdict}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows consoles are not always UTF-8
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    parser.add_argument("--cache-dir", default=".cache/fastembed")
    parser.add_argument("--dataset", default="demo")
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--overlap", type=int, default=150)
    parser.add_argument(
        "--heading-context", action="store_true", help="embed 'title > section' with the text"
    )
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--compare", choices=["heading-context"], default=None)
    parser.add_argument("--sweep", action="store_true", help="accuracy at other cut-offs")
    parser.add_argument("--show-misses", action="store_true")
    parser.add_argument("--fail-under", type=float, default=None, help="minimum hit@1")
    args = parser.parse_args()

    questions = json.loads((DATASETS / f"{args.dataset}.json").read_text(encoding="utf-8"))[
        "questions"
    ]
    embedder = build_embedder(args.model, args.cache_dir)
    cut_off = args.min_score if args.min_score is not None else default_min_score(embedder)
    n_ans = sum(q["expected"] is not None for q in questions)
    n_un = len(questions) - n_ans

    chunks, matrix = load_corpus(
        embedder,
        max_chars=args.max_chars,
        overlap=args.overlap,
        heading_context=args.heading_context,
    )
    print(f"Model {embedder.name}, {len(chunks)} chunks, cut-off {cut_off:.3f}")
    outcomes = evaluate(embedder, chunks, matrix, questions)
    label = "heading context " + ("on" if args.heading_context else "off")
    print_metrics(label, metrics(outcomes, cut_off), n_ans, n_un)

    if args.compare == "heading-context":
        other_chunks, other_matrix = load_corpus(
            embedder,
            max_chars=args.max_chars,
            overlap=args.overlap,
            heading_context=not args.heading_context,
        )
        other = evaluate(embedder, other_chunks, other_matrix, questions)
        other_label = "heading context " + ("off" if args.heading_context else "on")
        print_metrics(other_label, metrics(other, cut_off), n_ans, n_un)
        print(f"\nPaired difference: ({label}) minus ({other_label})")
        paired_difference(other, outcomes, cut_off)

    if args.sweep:
        print("\nCut-off sweep (accuracy over all questions / answerable / unanswerable):")
        for t in np.arange(0.40, 0.80, 0.025):
            all_ = np.mean([o.correct_at(t) for o in outcomes])
            ans = np.mean([o.correct_at(t) for o in outcomes if o.expected])
            un = np.mean([o.correct_at(t) for o in outcomes if not o.expected])
            marker = "  <- current" if abs(t - cut_off) < 1e-9 else ""
            print(f"  {t:.3f}  {all_:6.1%}  {ans:6.1%}  {un:6.1%}{marker}")

    if args.show_misses:
        print("\nMisses:")
        for o in outcomes:
            if not o.correct_at(cut_off):
                print(
                    f"  {o.qid:8} {o.top_score:.3f} expected={o.expected} "
                    f"got={o.top_file} [{o.top_section}]  {o.question}"
                )

    hit1 = np.mean([o.doc_rank == 1 for o in outcomes if o.expected])
    if args.fail_under is not None and hit1 < args.fail_under:
        print(f"\nFAIL: hit@1 {hit1:.1%} is below {args.fail_under:.1%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
