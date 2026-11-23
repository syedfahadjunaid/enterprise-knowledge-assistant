"""Vector search over the chunks the current user may read.

Notice what is *not* here: a permission filter. The session comes from
``Database.as_user(user)``, and Postgres row-level security adds "only documents this user has
a grant for" to every query on documents and chunks. A forgotten WHERE clause can't leak a
document, and tests/test_access_control.py proves the search honours the policies.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, DocumentStatus


@dataclass(frozen=True)
class Passage:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    heading: str | None
    page: int | None
    text: str
    score: float  # cosine similarity, higher is better


async def search_passages(
    session: AsyncSession,
    query_vector: np.ndarray | Sequence[float],
    *,
    embedding_model: str,
    top_k: int,
    min_score: float,
) -> list[Passage]:
    # With an HNSW index, Postgres finds nearest neighbours first and applies the RLS filter
    # afterwards. If the user can read only a few documents, the first batch of neighbours may
    # all be filtered out. Iterative scans keep searching until top_k permitted rows are found.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))

    distance = Chunk.embedding.cosine_distance(np.asarray(query_vector, dtype=np.float32))
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Document.title,
            Chunk.heading,
            Chunk.page,
            Chunk.text,
            (1 - distance).label("score"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.status == DocumentStatus.READY,
            Chunk.embedding_model == embedding_model,  # never compare vectors of two models
        )
        .order_by(distance)
        .limit(top_k)
    )
    rows = (await session.execute(stmt)).all()
    return [
        Passage(
            chunk_id=row.id,
            document_id=row.document_id,
            document_title=row.title,
            heading=row.heading,
            page=row.page,
            text=row.text,
            score=float(row.score),
        )
        for row in rows
        if float(row.score) >= min_score  # relevance cut-off ("not found"), not a security check
    ]
