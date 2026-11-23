"""The question-answering flow.

1. ``retrieve``: embed the question, search the chunks the user may read (row-level security),
   and write the audit record of which documents will be shown, all in one transaction. Only
   after that commits does anything reach the user.
2. ``stream``: send the passages to the browser, then stream the answer with its citations as
   server-sent events (SSE):

       event: sources   data: [{"index": 0, "document_title": ..., "text": ...}, ...]
       event: delta     data: {"text": "Employees get "}
       event: citation  data: {"source": 0, "cited_text": "..."}
       event: done      data: {"model": ..., "stop_reason": "end_turn", "no_llm_call": false}
       event: error     data: {"message": ...}          (only if the LLM fails mid-way)
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence

from app import audit
from app.answers.generators import (
    AnswerFinished,
    AnswerGenerator,
    AnswerUnavailableError,
    Citation,
    TextDelta,
)
from app.db import Database
from app.retrieval.embeddings import Embedder
from app.retrieval.search import Passage, search_passages
from app.security import User

log = logging.getLogger(__name__)

NOT_FOUND_ANSWER = (
    "I couldn't find anything about that in the documents you have access to. "
    "Try rephrasing the question, or ask the owner of the relevant documents to share them "
    "with you."
)
REFUSAL_ANSWER = "I can't help with that request."


class AnswerService:
    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        generator: AnswerGenerator,
        *,
        top_k: int,
        min_score: float,
    ) -> None:
        self._db = db
        self._embedder = embedder
        self._generator = generator
        self._top_k = top_k
        self._min_score = min_score

    async def retrieve(self, user: User, question: str) -> list[Passage]:
        # Embedding is CPU-bound (~10 ms): keep it off the event loop.
        vector = await asyncio.to_thread(self._embedder.embed_query, question)
        async with self._db.as_user(user) as session:
            passages = await search_passages(
                session,
                vector,
                embedding_model=self._embedder.name,
                top_k=self._top_k,
                min_score=self._min_score,
            )
            await audit.record(
                session,
                actor_sub=user.sub,
                actor_name=user.username,
                action=audit.AuditAction.ANSWER_SOURCES_SHOWN,
                document_ids=[p.document_id for p in passages],
                details={
                    "question": question[:500],
                    "passages": [
                        {
                            "chunk_id": str(p.chunk_id),
                            "document_id": str(p.document_id),
                            "document_title": p.document_title,
                            "score": round(p.score, 4),
                        }
                        for p in passages
                    ],
                },
            )
        return passages

    async def stream(self, question: str, passages: Sequence[Passage]) -> AsyncIterator[str]:
        yield sse("sources", [_source_json(i, p) for i, p in enumerate(passages)])
        if not passages:
            yield sse("delta", {"text": NOT_FOUND_ANSWER})
            yield sse("done", {"model": None, "stop_reason": "not_found", "no_llm_call": True})
            return
        try:
            async for event in self._generator.stream(question, passages):
                match event:
                    case TextDelta(text=text):
                        yield sse("delta", {"text": text})
                    case Citation(passage_index=index, cited_text=cited):
                        if 0 <= index < len(passages):
                            yield sse("citation", {"source": index, "cited_text": cited})
                    case AnswerFinished(model=model, stop_reason=stop_reason):
                        done = {"model": model, "stop_reason": stop_reason, "no_llm_call": False}
                        if stop_reason == "refusal":
                            done["replacement_text"] = REFUSAL_ANSWER
                        yield sse("done", done)
        except AnswerUnavailableError as e:
            log.warning("Answer generation failed: %s", e)
            yield sse("error", {"message": "The assistant is busy right now. Please try again."})


def sse(event: str, data: object) -> str:
    """One server-sent event. json.dumps escapes newlines, so ``data`` stays on one line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _source_json(index: int, passage: Passage) -> dict[str, object]:
    return {
        "index": index,
        "chunk_id": str(passage.chunk_id),
        "document_id": str(passage.document_id),
        "document_title": passage.document_title,
        "heading": passage.heading,
        "page": passage.page,
        "text": passage.text,
        "score": round(passage.score, 4),
    }
