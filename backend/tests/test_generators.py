"""Answer generators (no Docker, no API key, no cost).

The Claude generator is tested against a mocked HTTP transport that replays a recorded-style
event stream. The real ``anthropic`` SDK parses it, so these tests check our handling of the
actual SDK objects: text deltas, streamed citations, stop reasons and errors. They also check
what we *send*: search-result blocks with citations, effort, and the refusal fallback.
"""

import json
import uuid

import anthropic
import httpx2 as httpx  # the anthropic SDK uses httpx2, a fork of httpx with the same API
import pytest

from app.answers.generators import (
    FALLBACK_BETA,
    AnswerFinished,
    AnswerUnavailableError,
    Citation,
    ClaudeAnswerGenerator,
    OfflineAnswerGenerator,
    TextDelta,
)
from app.retrieval.search import Passage

PASSAGES = [
    Passage(uuid.uuid4(), uuid.uuid4(), "Handbook", "Remote work", None, "Two days a week.", 0.8),
    Passage(
        uuid.uuid4(),
        uuid.uuid4(),
        "Handbook",
        "Leave > Parental",
        None,
        "Sixteen weeks paid.\n\nCan be split into two blocks.",
        0.9,
    ),
]


def sse_body(stop_reason: str = "end_turn") -> bytes:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": "", "citations": []},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "citations_delta",
                "citation": {
                    "type": "search_result_location",
                    "cited_text": "Sixteen weeks paid.",
                    "source": "eka:document/x#chunk/y",
                    "title": "Handbook > Leave > Parental",
                    "search_result_index": 1,
                    "start_block_index": 0,
                    "end_block_index": 1,
                },
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Parents get sixteen paid weeks."},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": " Ask HR for details."},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 25},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def generator_with(handler) -> ClaudeAnswerGenerator:
    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return ClaudeAnswerGenerator(
        api_key="test-key",
        model="claude-opus-5-5",
        effort="low",
        max_tokens=16000,
        timeout_seconds=10,
        client=client,
    )


async def collect(generator, question: str = "How long is parental leave?") -> list:
    return [event async for event in generator.stream(question, PASSAGES)]


async def test_claude_stream_yields_text_then_citations_then_finish() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=sse_body(), headers={"content-type": "text/event-stream"}
        )

    events = await collect(generator_with(handler))

    assert events == [
        TextDelta("Parents get sixteen paid weeks."),
        Citation(1, "Sixteen weeks paid."),  # after the text it supports
        TextDelta(" Ask HR for details."),
        AnswerFinished(
            "claude-opus-5-5",
            "end_turn",
            {
                "input_tokens": 120,
                "output_tokens": 25,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ),
    ]


async def test_claude_request_sends_search_results_with_citations_and_fallback() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=sse_body())

    await collect(generator_with(handler), "How long is parental leave?")

    body = json.loads(requests[0].content)
    assert FALLBACK_BETA in requests[0].headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "low"}
    assert body["stream"] is True
    assert "thinking" not in body  # Opus 5.5 always thinks adaptively; effort is the control

    content = body["messages"][0]["content"]
    assert [block["type"] for block in content] == ["search_result", "search_result", "text"]
    assert content[-1]["text"] == "How long is parental leave?"
    second = content[1]
    assert second["citations"] == {"enabled": True}
    assert second["title"] == "Handbook > Leave > Parental"
    # Each paragraph is its own citable block.
    assert [b["text"] for b in second["content"]] == [
        "Sixteen weeks paid.",
        "Can be split into two blocks.",
    ]


async def test_refusal_is_reported_in_the_finish_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body(stop_reason="refusal"))

    events = await collect(generator_with(handler))
    assert events[-1].stop_reason == "refusal"


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_api_errors_become_answer_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "x"}}
        )

    with pytest.raises(AnswerUnavailableError):
        await collect(generator_with(handler))


async def test_offline_generator_quotes_and_cites_top_passages() -> None:
    events = await collect(OfflineAnswerGenerator())
    citations = [e for e in events if isinstance(e, Citation)]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))

    assert "Claude isn't configured" in text
    assert "Handbook > Leave > Parental: Sixteen weeks paid." in text
    assert [c.passage_index for c in citations] == [0, 1]
    assert events[-1] == AnswerFinished("offline", "end_turn")
