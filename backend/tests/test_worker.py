"""Ingestion worker robustness: retries, giving up, expired leases (needs Docker)."""

from collections.abc import Sequence

import numpy as np
import pytest
from sqlalchemy import create_engine, text

from app.db import Database
from app.ingestion.worker import IngestionWorker
from app.retrieval.embeddings import HashingEmbedder
from tests.conftest import ALICE, BOB, DatabaseInfo, auth
from tests.helpers import upload

pytestmark = pytest.mark.integration

DOC = "# Runbook\n\n## Failover\nPromote the replica, then repoint the service."


class FlakyEmbedder(HashingEmbedder):
    """Fails the first ``failures`` calls, like a model server having a bad minute."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("embedding backend unavailable")
        return super().embed_documents(texts)


def _sql(db: DatabaseInfo, statement: str, **params) -> list:
    engine = create_engine(db.superuser_url)
    with engine.begin() as conn:
        result = conn.execute(text(statement), params)
        rows = list(result) if result.returns_rows else []
    engine.dispose()
    return rows


def _make_jobs_due(db: DatabaseInfo) -> None:
    _sql(db, "UPDATE eka.ingestion_jobs SET run_after = now() WHERE status = 'QUEUED'")


async def _document(api, document_id: str) -> dict:
    return (await api.get(f"/api/documents/{document_id}", headers=auth(BOB))).json()


async def test_transient_failure_is_retried_with_backoff(
    api, worker_db: Database, clean_database: DatabaseInfo
) -> None:
    worker = IngestionWorker(worker_db, FlakyEmbedder(failures=1), clean_database.settings)
    document_id = (await upload(api, BOB, "runbook.md", DOC)).json()["id"]

    await worker.run_until_idle()
    document = await _document(api, document_id)
    assert document["status"] == "PENDING"
    assert document["error"].startswith("Attempt 1 failed, retrying")
    [(status, delay)] = _sql(
        clean_database,
        "SELECT status, extract(epoch FROM run_after - now()) FROM eka.ingestion_jobs",
    )
    assert status == "QUEUED" and 5 < delay <= 10  # first backoff step is 10 s

    assert await worker.run_until_idle() == 0  # not due yet
    _make_jobs_due(clean_database)
    await worker.run_until_idle()
    assert (await _document(api, document_id))["status"] == "READY"


async def test_worker_gives_up_after_max_attempts(
    api, worker_db: Database, clean_database: DatabaseInfo
) -> None:
    worker = IngestionWorker(worker_db, FlakyEmbedder(failures=99), clean_database.settings)
    document_id = (await upload(api, BOB, "runbook.md", DOC)).json()["id"]

    for _ in range(clean_database.settings.worker_max_attempts):
        _make_jobs_due(clean_database)
        await worker.run_until_idle()

    document = await _document(api, document_id)
    assert document["status"] == "FAILED"
    assert "embedding backend unavailable" in document["error"]
    events = (await api.get("/api/audit?limit=200", headers=auth(ALICE))).json()
    assert any(
        e["action"] == "DOCUMENT_INGESTION_FAILED" and document_id in map(str, e["document_ids"])
        for e in events
    )


async def test_expired_lease_is_reclaimed_and_the_stale_worker_cannot_overwrite(
    api, worker_db: Database, clean_database: DatabaseInfo
) -> None:
    settings = clean_database.settings
    slow = IngestionWorker(worker_db, HashingEmbedder(), settings)
    fast = IngestionWorker(worker_db, HashingEmbedder(), settings)
    document_id = (await upload(api, BOB, "runbook.md", DOC)).json()["id"]

    # The slow worker claims the job, then "hangs" until its lease runs out.
    stale_claim = await slow._claim()
    assert stale_claim is not None and stale_claim.attempt == 1
    _sql(clean_database, "UPDATE eka.ingestion_jobs SET locked_until = now() - interval '1 s'")

    # Another worker reclaims it (attempt 2) and finishes.
    assert await fast.run_until_idle() == 1
    assert (await _document(api, document_id))["status"] == "READY"

    # The slow worker wakes up and tries to save attempt 1: its results are dropped.
    await slow._process(stale_claim)
    [(attempts, status)] = _sql(clean_database, "SELECT attempts, status FROM eka.ingestion_jobs")
    assert (attempts, status) == (2, "SUCCEEDED")
    [(chunks,)] = _sql(clean_database, "SELECT count(*) FROM eka.chunks")
    assert chunks == 1
