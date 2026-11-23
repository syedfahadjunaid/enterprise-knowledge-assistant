"""Asking questions: permission-filtered retrieval, streamed citations, audit (needs Docker).

The test embedder (HashingEmbedder) matches shared words, so questions here reuse words from
the documents on purpose. Retrieval *quality* is measured separately by evals/retrieval.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest

from app.answers.generators import (
    AnswerEvent,
    AnswerFinished,
    AnswerUnavailableError,
    Citation,
    TextDelta,
)
from app.retrieval.search import Passage
from tests.conftest import ALICE, BOB, CAROL, DAVE, ERIN, auth
from tests.helpers import ask, upload_ready

pytestmark = pytest.mark.integration

SALARIES = """# Salary bands 2026

## Engineering
Senior engineer salary band: 95,000 to 120,000 EUR.
"""

ONCALL = """# On-call handbook

## Compensation
Each weekend on call earns one extra day off as compensation.
"""

HANDBOOK = """# Company handbook

## Remote work
Employees may work remotely up to three days per week.
"""


@pytest.fixture
async def corpus(api, worker) -> dict[str, dict]:
    return {
        "salaries": await upload_ready(
            api, worker, ALICE, "salary-bands.md", SALARIES, share_with=("group:/hr",)
        ),
        "oncall": await upload_ready(
            api, worker, BOB, "oncall.md", ONCALL, share_with=("group:/engineering",)
        ),
        "handbook": await upload_ready(
            api, worker, ALICE, "handbook.md", HANDBOOK, share_with=("everyone",)
        ),
    }


async def test_answer_streams_sources_text_citations_and_done(api, corpus) -> None:
    result = await ask(api, DAVE, "How much compensation for a weekend on call?")

    assert [e for e, _ in result.events][0] == "sources"
    assert result.sources[0]["document_title"] == "Oncall"
    assert result.sources[0]["heading"] == "On-call handbook > Compensation"
    assert "one extra day off" in result.sources[0]["text"]
    assert "Claude isn't configured" in result.answer  # offline mode in tests
    assert result.of("citation")[0]["source"] == 0
    assert result.done == {"model": "offline", "stop_reason": "end_turn", "no_llm_call": False}


async def test_users_never_see_passages_from_documents_they_cannot_read(api, corpus) -> None:
    question = "What is the senior engineer salary band?"
    hr = await ask(api, ALICE, question)
    assert hr.sources[0]["document_id"] == corpus["salaries"]["id"]

    for persona in (BOB, CAROL, DAVE, ERIN):
        result = await ask(api, persona, question)
        shown = {s["document_id"] for s in result.sources}
        assert corpus["salaries"]["id"] not in shown, persona.username
        assert "95,000" not in result.answer


async def test_nothing_found_answers_without_calling_the_llm(api, corpus) -> None:
    result = await ask(api, ERIN, "Quarterly zebra migration statistics?")
    assert result.sources == []
    assert "couldn't find anything" in result.answer
    assert result.done["no_llm_call"] is True


async def test_every_answer_is_audited_with_the_documents_shown(api, corpus) -> None:
    await ask(api, DAVE, "How much compensation for a weekend on call?")
    events = (await api.get("/api/audit?limit=200", headers=auth(ALICE))).json()
    shown = [e for e in events if e["action"] == "ANSWER_SOURCES_SHOWN"]
    latest = shown[0]
    assert latest["actor_name"] == "dave"
    assert corpus["oncall"]["id"] in latest["document_ids"]
    assert corpus["salaries"]["id"] not in latest["document_ids"]
    assert latest["details"]["question"] == "How much compensation for a weekend on call?"


@pytest.mark.parametrize(
    "body",
    [{"question": ""}, {"question": "   "}, {"question": "x" * 2001}, {"question": "hi", "x": 1}],
    ids=["empty", "blank", "too-long", "unknown-field"],
)
async def test_invalid_questions_are_422(api, body: dict) -> None:
    response = await api.post("/api/ask", headers=auth(BOB), json=body)
    assert response.status_code == 422


async def test_rate_limit_returns_429_with_retry_after(api, corpus) -> None:
    for _ in range(10):
        response = await api.post("/api/ask", headers=auth(ERIN), json={"question": "remote"})
        assert response.status_code == 200
    response = await api.post("/api/ask", headers=auth(ERIN), json={"question": "remote"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


class FailingGenerator:
    name = "failing"

    async def stream(
        self, question: str, passages: Sequence[Passage]
    ) -> AsyncIterator[AnswerEvent]:
        yield TextDelta("Partial ")
        raise AnswerUnavailableError("boom")


class RefusingGenerator:
    name = "refusing"

    async def stream(
        self, question: str, passages: Sequence[Passage]
    ) -> AsyncIterator[AnswerEvent]:
        yield TextDelta("Some text")
        yield Citation(0, "x")
        yield Citation(99, "out of range is dropped")
        yield AnswerFinished("claude-opus-5-5", "refusal")


@pytest.mark.parametrize("generator", [FailingGenerator()], indirect=False)
async def test_llm_failure_mid_stream_becomes_an_error_event(api, corpus) -> None:
    result = await ask(api, DAVE, "How much compensation for a weekend on call?")
    assert result.of("error") == [{"message": "The assistant is busy right now. Please try again."}]


@pytest.mark.parametrize("generator", [RefusingGenerator()])
async def test_refusal_tells_the_client_to_replace_the_text(api, corpus) -> None:
    result = await ask(api, DAVE, "How much compensation for a weekend on call?")
    assert result.done["stop_reason"] == "refusal"
    assert result.done["replacement_text"] == "I can't help with that request."
    assert [c["source"] for c in result.of("citation")] == [0]


async def test_unknown_document_ids_are_404(api) -> None:
    response = await api.get(f"/api/documents/{uuid.uuid4()}", headers=auth(BOB))
    assert response.status_code == 404
