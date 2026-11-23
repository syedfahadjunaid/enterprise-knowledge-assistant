"""The ingestion worker: turns uploaded files into searchable chunks, in the background.

Run it with ``python -m app.ingestion.worker`` (Docker Compose runs one container of it; you
can run several). How it gets work, and why it is safe to run many copies:

* The API inserts a row into ``ingestion_jobs`` in the same transaction as the document, so a
  job exists if and only if the upload committed (the "transactional outbox" idea).
* A trigger sends ``NOTIFY ingestion_jobs`` on insert, which wakes the worker immediately. If a
  notification is missed, the worker also polls every few seconds.
* A worker *claims* a job with ``SELECT ... FOR UPDATE SKIP LOCKED``: two workers never take the
  same job, and neither waits for the other.
* A claim is a lease (``locked_until``). If a worker dies mid-job, the lease runs out and another
  worker retries the job. Each claim bumps ``attempts``; a worker only saves its results if the
  job is still on *its* attempt, so a slow worker whose lease expired can't overwrite a newer
  attempt (a "fencing token").
* Failures that retrying can't fix (corrupt or empty file) fail the document at once. Other
  errors are retried with exponential backoff, up to ``worker_max_attempts`` times.

The worker uses the ``eka_worker`` database login, which bypasses row-level security: it must
read every uploaded document, whoever may read it. It never serves user requests.
"""

import asyncio
import contextlib
import logging
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from sqlalchemy import delete, func, insert, select, text, update

from app import audit
from app.config import Settings
from app.db import Database, create_engine
from app.ingestion.chunking import ChunkDraft, chunk_blocks, embedding_text
from app.ingestion.parsers import DocumentParseError, parse_document
from app.models import (
    Chunk,
    Document,
    DocumentFile,
    DocumentStatus,
    FileType,
    IngestionJob,
    JobStatus,
)
from app.retrieval.embeddings import Embedder, build_embedder

log = logging.getLogger("eka.worker")

CHANNEL = "ingestion_jobs"
MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    document_id: uuid.UUID
    attempt: int
    max_attempts: int


class IngestionWorker:
    def __init__(self, db: Database, embedder: Embedder, settings: Settings) -> None:
        self._db = db
        self._embedder = embedder
        self._settings = settings

    async def run_once(self) -> bool:
        """Claims and processes at most one due job. Returns False if there was none."""
        job = await self._claim()
        if job is None:
            return False
        if job.attempt > job.max_attempts:
            # Only reachable when workers kept dying on this job (expired leases).
            await self._fail(
                job, "Gave up: the worker stopped during every attempt", permanent=True
            )
            return True
        await self._process(job)
        return True

    async def run_until_idle(self) -> int:
        """Processes due jobs until none are left (used by tests and at startup)."""
        processed = 0
        while await self.run_once():
            processed += 1
        return processed

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("Ingestion worker started (embedding model %s)", self._embedder.name)
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    **_conninfo(self._settings), autocommit=True
                ) as listener:
                    await listener.execute(f"LISTEN {CHANNEL}")
                    while not stop.is_set():
                        await self.run_until_idle()
                        await _wait_for_notification(
                            listener, stop, self._settings.worker_poll_seconds
                        )
            except (psycopg.OperationalError, OSError) as e:
                log.warning("Database connection problem (%s); retrying shortly", e)
                await _sleep_or_stop(stop, self._settings.worker_poll_seconds)
        log.info("Ingestion worker stopped")

    # ----------------------------------------------------------------------------- steps

    async def _claim(self) -> ClaimedJob | None:
        async with self._db.transaction() as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE eka.ingestion_jobs j
                        SET status = 'RUNNING',
                            attempts = j.attempts + 1,
                            locked_until = now() + make_interval(secs => :lease),
                            updated_at = now()
                        WHERE j.id = (
                            SELECT id FROM eka.ingestion_jobs
                            WHERE (status = 'QUEUED' AND run_after <= now())
                               OR (status = 'RUNNING' AND locked_until < now())
                            ORDER BY run_after
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1)
                        RETURNING j.id, j.document_id, j.attempts, j.max_attempts
                        """
                    ),
                    {"lease": self._settings.worker_lease_seconds},
                )
            ).one_or_none()
            if row is None:
                return None
            await session.execute(
                update(Document)
                .where(Document.id == row.document_id)
                .values(status=DocumentStatus.PROCESSING, updated_at=datetime.now(UTC))
            )
        return ClaimedJob(row.id, row.document_id, row.attempts, row.max_attempts)

    async def _process(self, job: ClaimedJob) -> None:
        async with self._db.transaction() as session:
            row = (
                await session.execute(
                    select(Document.title, Document.file_type, DocumentFile.data)
                    .join(DocumentFile, DocumentFile.document_id == Document.id)
                    .where(Document.id == job.document_id)
                )
            ).one_or_none()
        if row is None:
            log.info("Document %s was deleted before ingestion", job.document_id)
            return

        log.info("Ingesting document %s (attempt %d)", job.document_id, job.attempt)
        try:
            # Parsing and embedding are CPU-bound: run them in a thread so the event loop
            # (and the lease bookkeeping) stays responsive.
            parsed = await asyncio.to_thread(parse_document, row.data, FileType(row.file_type))
            drafts = chunk_blocks(
                parsed.blocks,
                max_chars=self._settings.chunk_max_chars,
                overlap_chars=self._settings.chunk_overlap_chars,
            )
            texts = [
                embedding_text(row.title, d) if self._settings.embed_heading_context else d.text
                for d in drafts
            ]
            vectors = await asyncio.to_thread(self._embedder.embed_documents, texts)
        except DocumentParseError as e:
            await self._fail(job, str(e), permanent=True)
        except Exception as e:  # noqa: BLE001 - anything else may be transient: retry
            log.exception("Ingestion of %s failed", job.document_id)
            await self._fail(job, f"{type(e).__name__}: {e}", permanent=False)
        else:
            await self._complete(job, drafts, vectors)

    async def _complete(self, job: ClaimedJob, drafts: list[ChunkDraft], vectors: list) -> None:
        async with self._db.transaction() as session:
            if not await _still_ours(session, job):
                return
            await session.execute(delete(Chunk).where(Chunk.document_id == job.document_id))
            await session.execute(
                insert(Chunk),
                [
                    {
                        "id": uuid.uuid4(),
                        "document_id": job.document_id,
                        "ordinal": d.ordinal,
                        "text": d.text,
                        "heading": d.heading,
                        "page": d.page,
                        "embedding_model": self._embedder.name,
                        "embedding": v,
                    }
                    for d, v in zip(drafts, vectors, strict=True)
                ],
            )
            now = datetime.now(UTC)
            await session.execute(
                update(Document)
                .where(Document.id == job.document_id)
                .values(
                    status=DocumentStatus.READY,
                    chunk_count=len(drafts),
                    error=None,
                    updated_at=now,
                )
            )
            await session.execute(
                update(IngestionJob)
                .where(IngestionJob.id == job.id)
                .values(status=JobStatus.SUCCEEDED, locked_until=None, updated_at=now)
            )
            await audit.record(
                session,
                actor_sub=audit.SYSTEM_ACTOR[0],
                actor_name=audit.SYSTEM_ACTOR[1],
                action=audit.AuditAction.DOCUMENT_INGESTED,
                document_ids=[job.document_id],
                details={"chunks": len(drafts), "embedding_model": self._embedder.name},
            )
        log.info("Document %s is ready (%d chunks)", job.document_id, len(drafts))

    async def _fail(self, job: ClaimedJob, error: str, *, permanent: bool) -> None:
        error = error[:MAX_ERROR_CHARS]
        final = permanent or job.attempt >= job.max_attempts
        async with self._db.transaction() as session:
            if not await _still_ours(session, job):
                return
            now = datetime.now(UTC)
            if final:
                job_values = {"status": JobStatus.FAILED, "locked_until": None}
                doc_values = {"status": DocumentStatus.FAILED, "error": error}
            else:
                delay = min(10 * 2 ** (job.attempt - 1), 600)  # 10 s, 20 s, 40 s, ... max 10 min
                job_values = {
                    "status": JobStatus.QUEUED,
                    "locked_until": None,
                    "run_after": func.now() + timedelta(seconds=delay),
                }
                doc_values = {
                    "status": DocumentStatus.PENDING,
                    "error": f"Attempt {job.attempt} failed, retrying: {error}",
                }
            await session.execute(
                update(IngestionJob)
                .where(IngestionJob.id == job.id)
                .values(**job_values, last_error=error, updated_at=now)
            )
            await session.execute(
                update(Document)
                .where(Document.id == job.document_id)
                .values(**doc_values, updated_at=now)
            )
            if final:
                await audit.record(
                    session,
                    actor_sub=audit.SYSTEM_ACTOR[0],
                    actor_name=audit.SYSTEM_ACTOR[1],
                    action=audit.AuditAction.DOCUMENT_INGESTION_FAILED,
                    document_ids=[job.document_id],
                    details={"error": error, "attempts": job.attempt},
                )
        log.warning(
            "Ingestion of %s failed (%s): %s",
            job.document_id,
            "giving up" if final else "will retry",
            error,
        )


async def _still_ours(session, job: ClaimedJob) -> bool:
    """Locks the job row and checks that no other worker has taken it over meanwhile."""
    row = (
        await session.execute(
            select(IngestionJob.status, IngestionJob.attempts)
            .where(IngestionJob.id == job.id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        log.info("Job %s disappeared (document deleted); dropping results", job.id)
        return False
    if row.status != JobStatus.RUNNING or row.attempts != job.attempt:
        log.warning("Job %s was taken over by another attempt; dropping results", job.id)
        return False
    return True


async def _wait_for_notification(
    listener: psycopg.AsyncConnection, stop: asyncio.Event, max_wait: float
) -> None:
    """Returns on the first NOTIFY, after ``max_wait`` seconds, or when asked to stop."""
    notified = asyncio.ensure_future(_first_notification(listener, max_wait))
    stopped = asyncio.ensure_future(stop.wait())
    _, pending = await asyncio.wait({notified, stopped}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _first_notification(listener: psycopg.AsyncConnection, max_wait: float) -> None:
    async for _ in listener.notifies(timeout=max_wait, stop_after=1):
        return


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _conninfo(settings: Settings) -> dict[str, object]:
    return {
        "host": settings.db_host,
        "port": settings.db_port,
        "dbname": settings.db_name,
        "user": "eka_worker",
        "password": settings.eka_worker_db_password.get_secret_value(),
        "application_name": "eka-worker-listener",
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    embedder = build_embedder(settings.embedding_model, settings.embedding_cache_dir)
    engine = create_engine(settings, "worker")
    worker = IngestionWorker(Database(engine), embedder, settings)

    stop = asyncio.Event()
    if sys.platform != "win32":  # Windows has no SIGTERM handlers in asyncio
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run_forever(stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg's async mode needs the selector event loop on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
