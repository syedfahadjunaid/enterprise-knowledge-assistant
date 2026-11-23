"""Answer generators: turn a question + permitted passages into a stream of answer events.

Two implementations of one ``AnswerGenerator`` protocol:

* ``ClaudeAnswerGenerator`` streams from Claude through the official ``anthropic`` SDK, with
  native citations (see prompts.py).
* ``OfflineAnswerGenerator`` is used when no ``ANTHROPIC_API_KEY`` is set: no LLM call, no
  cost. It streams the best passages as a short extractive answer, cited the same way, so the
  whole product (permissions, citations, audit, streaming) works without a key.

Both yield the same three event types, which the service turns into server-sent events.
"""

import logging
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import anthropic

from app.answers.prompts import SYSTEM_PROMPT, build_user_content
from app.retrieval.search import Passage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Citation:
    """Marks the text streamed so far as supported by passage ``passage_index`` (0-based)."""

    passage_index: int
    cited_text: str


@dataclass(frozen=True)
class AnswerFinished:
    model: str  # the model that actually answered (may be a fallback model)
    # Why generation stopped: "end_turn", "max_tokens", "refusal", ...
    stop_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)


AnswerEvent = TextDelta | Citation | AnswerFinished


class AnswerUnavailableError(Exception):
    """The LLM could not be reached or returned an error."""


class AnswerGenerator(Protocol):
    name: str

    def stream(self, question: str, passages: Sequence[Passage]) -> AsyncIterator[AnswerEvent]: ...


# The beta that lets the API retry a refused request on a fallback model inside the same
# call. "default" lets the API pick the fallback per refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeAnswerGenerator:
    def __init__(
        self,
        api_key: str,
        model: str,
        effort: str,
        max_tokens: int,
        timeout_seconds: float,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # The SDK retries 408/409/429/5xx and connection errors itself (max_retries).
        # Tests pass a client whose HTTP transport is mocked.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=2
        )
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self.name = model

    async def stream(
        self, question: str, passages: Sequence[Passage]
    ) -> AsyncIterator[AnswerEvent]:
        pending: list[Citation] = []
        try:
            async with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                # Thinking depth. Opus 5.5 always thinks adaptively; effort is the control.
                output_config={"effort": self._effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_content(question, passages)}],
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(delta.text)
                        elif (
                            delta.type == "citations_delta"
                            and delta.citation.type == "search_result_location"
                        ):
                            # Citations arrive at the start of the text block they support.
                            # Hold them until the block ends, so the UI can put the marker
                            # after the cited sentence rather than before it.
                            pending.append(
                                Citation(
                                    delta.citation.search_result_index,
                                    delta.citation.cited_text,
                                )
                            )
                    elif event.type == "content_block_stop" and pending:
                        for citation in pending:
                            yield citation
                        pending.clear()
                final = await stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise AnswerUnavailableError("LLM rate limited") from e
        except anthropic.APIStatusError as e:
            log.error("LLM error status=%s request_id=%s", e.status_code, e.request_id)
            raise AnswerUnavailableError(f"LLM returned {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise AnswerUnavailableError("LLM unreachable") from e

        # Check stop_reason before trusting the content: a refusal (from every model in the
        # fallback chain) means the streamed text must be discarded by the client.
        if final.stop_reason == "max_tokens":
            log.warning("Answer truncated at max_tokens=%s", self._max_tokens)
        yield AnswerFinished(final.model, final.stop_reason, _usage(final.usage))


class OfflineAnswerGenerator:
    """No LLM: quotes the opening of the top passages, each with a citation."""

    name = "offline"

    def __init__(self, max_passages: int = 3, max_chars: int = 320) -> None:
        self._max_passages = max_passages
        self._max_chars = max_chars

    async def stream(
        self, question: str, passages: Sequence[Passage]
    ) -> AsyncIterator[AnswerEvent]:
        yield TextDelta(
            "Claude isn't configured on this server (no ANTHROPIC_API_KEY), so here are the "
            "most relevant passages from documents you can access:\n"
        )
        for index, passage in enumerate(passages[: self._max_passages]):
            label = passage.document_title + (f" > {passage.heading}" if passage.heading else "")
            yield TextDelta(f"\n- {label}: {_excerpt(passage.text, self._max_chars)}")
            yield Citation(index, passage.text)
        yield AnswerFinished(model="offline", stop_reason="end_turn")


def _excerpt(text: str, max_chars: int) -> str:
    """The first sentences of ``text`` that fit in ``max_chars``."""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    sentences = re.split(r"(?<=[.!?])\s+", flat)
    excerpt = ""
    for sentence in sentences:
        if len(excerpt) + len(sentence) + 1 > max_chars:
            break
        excerpt = f"{excerpt} {sentence}".strip()
    return excerpt or flat[:max_chars].rsplit(" ", 1)[0] + "…"


def _usage(usage: object) -> dict[str, int]:
    """Token counts from the API response (cache fields are None when caching wasn't used)."""
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {f: getattr(usage, f, None) or 0 for f in fields}
