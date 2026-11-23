"""Permission eval: the demo corpus, every eval question, every persona (needs Docker).

Loads the real demo documents (demo-docs/manifest.json) through the API and the worker, then:

1. For each persona, searches with no relevance cut-off and a huge top_k, so *every* chunk the
   database lets them see comes back. The set of documents must equal the table below, which is
   written out by hand from the manifest (not computed by the code under test).
2. Asks every eval question as every persona through the real ``AnswerService.retrieve`` and
   checks that no returned passage comes from a document outside that persona's set.

The leak count must be exactly zero. This complements the retrieval eval, which measures
ranking quality and deliberately ignores permissions.
"""

import json
from pathlib import Path

import pytest

from app.answers.service import AnswerService
from app.db import Database
from app.retrieval.embeddings import HashingEmbedder
from app.retrieval.search import search_passages
from tests.conftest import ALICE, BOB, CAROL, DAVE, ERIN, TEST_VERIFIER, make_token
from tests.helpers import upload_ready

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "demo-docs"
QUESTIONS = json.loads(
    (REPO / "backend" / "evals" / "retrieval" / "datasets" / "demo.json").read_text("utf-8")
)["questions"]

EVERYONE_DOCS = {
    "company-handbook.md",
    "information-security-policy.html",
    "travel-and-expense-policy.docx",
}
EXPECTED_VISIBLE = {
    "alice": EVERYONE_DOCS | {"salary-bands-2026.md"},  # /hr
    "bob": EVERYONE_DOCS | {"oncall-handbook.md", "platform-incident-runbook.pdf"},  # platform
    "carol": EVERYONE_DOCS | {"q3-budget-forecast.html", "procurement-process.md"},  # /finance
    "dave": EVERYONE_DOCS | {"oncall-handbook.md"},  # /engineering, not the platform subgroup
    "erin": EVERYONE_DOCS,  # no groups
}
PERSONAS = {p.username: p for p in (ALICE, BOB, CAROL, DAVE, ERIN)}


@pytest.fixture
async def demo_corpus(api, worker) -> dict[str, str]:
    """Uploads the demo documents as their manifest uploaders; returns document id -> file."""
    manifest = json.loads((DEMO / "manifest.json").read_text("utf-8"))
    ids: dict[str, str] = {}
    for doc in manifest["documents"]:
        document = await upload_ready(
            api,
            worker,
            PERSONAS[doc["uploader"]],
            doc["file"],
            (DEMO / doc["file"]).read_bytes(),
            share_with=tuple(doc["share_with"]),
        )
        ids[document["id"]] = doc["file"]
    return ids


@pytest.mark.parametrize("username", sorted(EXPECTED_VISIBLE))
async def test_search_reaches_exactly_the_granted_documents(
    app_db: Database, demo_corpus: dict[str, str], username: str
) -> None:
    user = TEST_VERIFIER.verify(make_token(PERSONAS[username]))
    embedder = HashingEmbedder()
    async with app_db.as_user(user) as session:
        passages = await search_passages(
            session,
            embedder.embed_query("anything"),
            embedding_model=embedder.name,
            top_k=10_000,
            min_score=-1.0,  # no relevance cut-off: everything the database allows
        )
    reachable = {demo_corpus[str(p.document_id)] for p in passages}
    assert reachable == EXPECTED_VISIBLE[username]


async def test_no_eval_question_leaks_a_document_to_any_persona(
    app_db: Database, demo_corpus: dict[str, str]
) -> None:
    service = AnswerService(
        app_db,
        HashingEmbedder(),
        generator=None,
        top_k=6,
        min_score=-1.0,  # type: ignore[arg-type]
    )
    leaks: list[str] = []
    checked = 0
    for username, allowed in EXPECTED_VISIBLE.items():
        user = TEST_VERIFIER.verify(make_token(PERSONAS[username]))
        for q in QUESTIONS:
            for passage in await service.retrieve(user, q["question"]):
                checked += 1
                file = demo_corpus[str(passage.document_id)]
                if file not in allowed:
                    leaks.append(f"{username} saw {file} for {q['id']}")
    assert checked >= len(QUESTIONS) * len(EXPECTED_VISIBLE) * 3  # the check had real work to do
    assert leaks == []
