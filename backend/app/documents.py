"""Documents: upload, list, read and delete, with the business rules for sharing.

Who may *read* a document is enforced by the database (row-level security), so every query
here already sees only permitted rows. This module adds the rules about *changing* things:

* Uploading needs the ``kb-editor`` or ``kb-admin`` role (403 otherwise).
* You may share with groups you belong to (or their parent groups). Only ``kb-admin`` may share
  with everyone. Anything else is a 422 naming the offending principal.
* The uploader always keeps access (an implicit ``user:<sub>`` grant).
* Deleting needs to be the owner or ``kb-admin``. A document you can't see is a 404, never a 403,
  so the API doesn't reveal that it exists.
"""

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.db import Database
from app.ingestion.filetypes import UnsupportedFileError, detect_file_type
from app.models import (
    Document,
    DocumentFile,
    DocumentGrant,
    DocumentStatus,
    IngestionJob,
    JobStatus,
)
from app.problems import forbidden, invalid, not_found
from app.schemas import DocumentOut
from app.security import User

MAX_TITLE_CHARS = 300
MAX_FILENAME_CHARS = 255


class DocumentService:
    def __init__(self, db: Database, *, max_upload_bytes: int, max_attempts: int) -> None:
        self._db = db
        self._max_upload_bytes = max_upload_bytes
        self._max_attempts = max_attempts

    async def upload(
        self,
        user: User,
        *,
        filename: str,
        data: bytes,
        title: str | None,
        share_with: Sequence[str],
    ) -> DocumentOut:
        if not user.can_upload:
            raise forbidden("Uploading documents needs the kb-editor role")

        filename = PurePosixPath((filename or "").replace("\\", "/")).name[:MAX_FILENAME_CHARS]
        if len(data) > self._max_upload_bytes:
            limit_mb = self._max_upload_bytes // (1024 * 1024)
            raise invalid("file", f"The file is larger than {limit_mb} MB")
        try:
            file_type = detect_file_type(filename, data)
        except UnsupportedFileError as e:
            raise invalid("file", str(e)) from e

        title = " ".join((title or "").split()) or _title_from_filename(filename)
        if len(title) > MAX_TITLE_CHARS:
            raise invalid("title", f"The title must be at most {MAX_TITLE_CHARS} characters")

        targets = list(dict.fromkeys(p.strip() for p in share_with if p.strip()))
        allowed = set(user.share_targets)
        for principal in targets:
            if principal not in allowed:
                raise invalid("share_with", f"You can't share with '{principal}'")

        now = datetime.now(UTC)
        document_id = uuid.uuid4()
        owner_principal = f"user:{user.sub}"
        async with self._db.as_user(user) as session:
            # Plain INSERTs without RETURNING: the new row only becomes readable (under RLS)
            # once its grants exist, a few statements later in this same transaction.
            await session.execute(
                insert(Document).values(
                    id=document_id,
                    title=title,
                    filename=filename,
                    file_type=file_type.value,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    status=DocumentStatus.PENDING,
                    error=None,
                    chunk_count=0,
                    owner_sub=user.sub,
                    owner_name=user.username,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.execute(insert(DocumentFile).values(document_id=document_id, data=data))
            await session.execute(
                insert(DocumentGrant),
                [
                    {
                        "document_id": document_id,
                        "principal": principal,
                        "granted_by": user.sub,
                        "created_at": now,
                    }
                    for principal in [owner_principal, *targets]
                ],
            )
            # The job commits with the document (transactional outbox); a trigger NOTIFYs
            # the worker on commit.
            await session.execute(
                insert(IngestionJob).values(
                    id=uuid.uuid4(),
                    document_id=document_id,
                    status=JobStatus.QUEUED,
                    attempts=0,
                    max_attempts=self._max_attempts,
                    run_after=now,
                    locked_until=None,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await audit.record(
                session,
                actor_sub=user.sub,
                actor_name=user.username,
                action=audit.AuditAction.DOCUMENT_UPLOADED,
                document_ids=[document_id],
                details={"title": title, "filename": filename, "shared_with": targets},
            )
            return await self._get(session, user, document_id)

    async def list(self, user: User) -> list[DocumentOut]:
        async with self._db.as_user(user) as session:
            documents = (
                await session.scalars(select(Document).order_by(Document.created_at.desc()))
            ).all()
            grants = await _grants_by_document(session, [d.id for d in documents])
            return [_to_out(d, grants[d.id], user) for d in documents]

    async def get(self, user: User, document_id: uuid.UUID) -> DocumentOut:
        async with self._db.as_user(user) as session:
            return await self._get(session, user, document_id)

    async def delete(self, user: User, document_id: uuid.UUID) -> None:
        async with self._db.as_user(user) as session:
            document = await session.get(Document, document_id)
            if document is None:
                raise not_found()
            if not _can_delete(document, user):
                raise forbidden("Only the owner or a kb-admin can delete this document")
            # Chunks, grants, the file and the job go with it (ON DELETE CASCADE).
            await session.execute(delete(Document).where(Document.id == document_id))
            await audit.record(
                session,
                actor_sub=user.sub,
                actor_name=user.username,
                action=audit.AuditAction.DOCUMENT_DELETED,
                document_ids=[document_id],
                details={"title": document.title, "owner": document.owner_name},
            )

    async def _get(self, session: AsyncSession, user: User, document_id: uuid.UUID) -> DocumentOut:
        document = await session.get(Document, document_id)
        if document is None:
            raise not_found()
        grants = await _grants_by_document(session, [document_id])
        return _to_out(document, grants[document_id], user)


async def _grants_by_document(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    result: dict[uuid.UUID, list[str]] = defaultdict(list)
    if not document_ids:
        return result
    rows = await session.execute(
        select(DocumentGrant.document_id, DocumentGrant.principal)
        .where(DocumentGrant.document_id.in_(document_ids))
        .order_by(DocumentGrant.principal)
    )
    for document_id, principal in rows:
        result[document_id].append(principal)
    return result


def _to_out(document: Document, principals: list[str], user: User) -> DocumentOut:
    owner = f"user:{document.owner_sub}"
    return DocumentOut(
        id=document.id,
        title=document.title,
        filename=document.filename,
        file_type=document.file_type,
        size_bytes=document.size_bytes,
        status=document.status,
        error=document.error,
        chunk_count=document.chunk_count,
        owner_name=document.owner_name,
        is_owner=document.owner_sub == user.sub,
        can_delete=_can_delete(document, user),
        shared_with=[p for p in principals if p != owner],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _can_delete(document: Document, user: User) -> bool:
    return document.owner_sub == user.sub or user.is_admin


def _title_from_filename(filename: str) -> str:
    stem = PurePosixPath(filename).stem.replace("_", " ").replace("-", " ")
    stem = " ".join(stem.split())
    return (stem[:1].upper() + stem[1:]) if stem else "Untitled document"
