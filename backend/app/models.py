"""Database tables, mapped with SQLAlchemy 2.0's typed ORM (``Mapped[...]``).

The schema itself, including the row-level security policies, is created by the Alembic
migrations in ``migrations/versions``; these classes only describe it to Python.
``tests/test_schema.py`` checks that the two agree (like Hibernate's ``ddl-auto: validate``).
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, DateTime, ForeignKey, Integer, LargeBinary, MetaData, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# bge-small-en-v1.5 produces 384-dimensional vectors. The column has a fixed size so it can have
# an HNSW index; a model with another size needs a migration.
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    metadata = MetaData(schema="eka")


class DocumentStatus(StrEnum):
    PENDING = "PENDING"  # uploaded, waiting for the ingestion worker
    PROCESSING = "PROCESSING"  # a worker is parsing and embedding it
    READY = "READY"  # searchable
    FAILED = "FAILED"  # could not be ingested; see Document.error


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer)
    owner_sub: Mapped[str] = mapped_column(Text)
    owner_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentFile(Base):
    """The original upload. Kept in its own table so listing documents never loads file bytes."""

    __tablename__ = "document_files"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[bytes] = mapped_column(LargeBinary)


class DocumentGrant(Base):
    """Who may read a document: one row per principal (``user:..``, ``group:/..``, ``everyone``)."""

    __tablename__ = "document_grants"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    principal: Mapped[str] = mapped_column(Text, primary_key=True)
    granted_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Chunk(Base):
    """A passage of a document plus its embedding. What the search returns and Claude cites."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)  # position within the document
    text: Mapped[str] = mapped_column(Text)
    heading: Mapped[str | None] = mapped_column(Text)  # "Section > Subsection"
    page: Mapped[int | None] = mapped_column(Integer)  # PDFs only, 1-based
    embedding_model: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM))


class IngestionJob(Base):
    """A unit of background work. The worker claims jobs with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Append-only record of who did what, and who was shown which documents."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_sub: Mapped[str] = mapped_column(Text)
    actor_name: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
