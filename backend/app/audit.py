"""The audit log: an append-only record of uploads, deletions and, above all, which documents
each user was shown.

``record`` must be called inside the transaction of the change (or the search) it describes,
so the audit row and the action commit or roll back together. For answers this means the row
listing the source documents is committed *before* any passage is streamed to the user.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


class AuditAction(StrEnum):
    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    DOCUMENT_INGESTED = "DOCUMENT_INGESTED"
    DOCUMENT_INGESTION_FAILED = "DOCUMENT_INGESTION_FAILED"
    ANSWER_SOURCES_SHOWN = "ANSWER_SOURCES_SHOWN"


SYSTEM_ACTOR = ("system:ingestion-worker", "Ingestion worker")


async def record(
    session: AsyncSession,
    *,
    actor_sub: str,
    actor_name: str,
    action: AuditAction,
    document_ids: Sequence[uuid.UUID] = (),
    details: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        insert(AuditEvent).values(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            actor_sub=actor_sub,
            actor_name=actor_name,
            action=action.value,
            document_ids=list(dict.fromkeys(document_ids)),  # de-duplicated, order kept
            details=details or {},
        )
    )


async def recent(session: AsyncSession, limit: int) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.occurred_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())
