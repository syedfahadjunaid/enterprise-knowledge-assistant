"""Uploading, ingesting, listing and deleting documents through the API (needs Docker)."""

import pytest
from sqlalchemy import create_engine, text

from app.ingestion.worker import IngestionWorker
from tests.conftest import ALICE, BOB, CAROL, DAVE, ERIN, DatabaseInfo, auth
from tests.helpers import upload, upload_ready
from tests.test_parsing import make_docx, make_pdf

pytestmark = pytest.mark.integration

HANDBOOK = """# On-call handbook

## Paging
Pages go to the primary on-call engineer first and escalate after fifteen minutes.

## Compensation
Each weekend on call earns one extra day off.
"""


async def test_upload_is_accepted_then_ingested_by_the_worker(api, worker) -> None:
    response = await upload(api, BOB, "oncall.md", HANDBOOK, share_with=("group:/engineering",))
    assert response.status_code == 202
    document = response.json()
    assert document["status"] == "PENDING"
    assert document["title"] == "Oncall"
    assert document["shared_with"] == ["group:/engineering"]
    assert document["is_owner"] and document["can_delete"]

    assert await worker.run_until_idle() == 1

    ready = (await api.get(f"/api/documents/{document['id']}", headers=auth(BOB))).json()
    assert ready["status"] == "READY"
    assert ready["chunk_count"] == 2  # one per section
    assert ready["error"] is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [("travel.docx", make_docx()), ("runbook.pdf", make_pdf(["Promote the replica."]))],
)
async def test_binary_formats_are_ingested(api, worker, filename: str, content: bytes) -> None:
    document = await upload_ready(api, worker, BOB, filename, content)
    assert document["chunk_count"] >= 1


async def test_readers_cannot_upload(api) -> None:
    response = await upload(api, DAVE, "notes.md", "# Notes\nhello")
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("filename", "content", "share_with", "field"),
    [
        ("virus.exe", b"MZ", (), "file"),
        ("fake.pdf", b"not a pdf", (), "file"),
        ("ok.md", b"# ok", ("group:/finance",), "share_with"),  # bob isn't in finance
        ("ok.md", b"# ok", ("everyone",), "share_with"),  # only admins share with everyone
    ],
)
async def test_invalid_uploads_are_422_with_field_errors(
    api, filename: str, content: bytes, share_with: tuple[str, ...], field: str
) -> None:
    response = await upload(api, BOB, filename, content, share_with=share_with)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"][0]["field"] == field


async def test_oversized_upload_is_rejected(api) -> None:
    # Settings.max_upload_bytes defaults to 20 MB
    too_big = b"# big\n" + b"a" * (20 * 1024 * 1024)
    response = await upload(api, BOB, "big.md", too_big)
    assert response.status_code == 422
    assert "larger than 20 MB" in response.json()["errors"][0]["message"]


async def test_admins_can_share_with_everyone(api, worker) -> None:
    await upload_ready(api, worker, ALICE, "handbook.md", HANDBOOK, share_with=("everyone",))
    listed = (await api.get("/api/documents", headers=auth(ERIN))).json()
    assert [d["title"] for d in listed] == ["Handbook"]
    assert listed[0]["is_owner"] is False and listed[0]["can_delete"] is False


async def test_listing_and_reading_follow_the_grants(api, worker) -> None:
    eng = await upload_ready(
        api, worker, BOB, "eng.md", HANDBOOK, share_with=("group:/engineering",)
    )
    platform = await upload_ready(
        api, worker, BOB, "platform.md", HANDBOOK, share_with=("group:/engineering/platform",)
    )
    finance = await upload_ready(api, worker, CAROL, "budget.md", "# Budget\nQ3 numbers.")

    def titles(response) -> set[str]:
        return {d["title"] for d in response.json()}

    assert titles(await api.get("/api/documents", headers=auth(BOB))) == {"Eng", "Platform"}
    # dave is in /engineering but not in the /engineering/platform subgroup
    assert titles(await api.get("/api/documents", headers=auth(DAVE))) == {"Eng"}
    assert titles(await api.get("/api/documents", headers=auth(CAROL))) == {"Budget"}
    assert titles(await api.get("/api/documents", headers=auth(ERIN))) == set()

    # Hidden documents are 404, exactly like documents that don't exist.
    assert (
        await api.get(f"/api/documents/{platform['id']}", headers=auth(DAVE))
    ).status_code == 404
    assert (await api.get(f"/api/documents/{finance['id']}", headers=auth(BOB))).status_code == 404
    assert (await api.get(f"/api/documents/{eng['id']}", headers=auth(DAVE))).status_code == 200


async def test_delete_rules(api, worker) -> None:
    doc = await upload_ready(
        api, worker, BOB, "eng.md", HANDBOOK, share_with=("group:/engineering",)
    )
    url = f"/api/documents/{doc['id']}"

    assert (await api.delete(url, headers=auth(CAROL))).status_code == 404  # can't see it
    assert (await api.delete(url, headers=auth(DAVE))).status_code == 403  # sees it, not owner
    assert (await api.delete(url, headers=auth(BOB))).status_code == 204
    assert (await api.get(url, headers=auth(BOB))).status_code == 404


async def test_admin_can_delete_documents_they_can_see(api, worker) -> None:
    doc = await upload_ready(api, worker, ALICE, "h.md", HANDBOOK, share_with=("group:/hr",))
    other = await upload_ready(api, worker, BOB, "b.md", HANDBOOK)  # private to bob
    assert (await api.delete(f"/api/documents/{doc['id']}", headers=auth(ALICE))).status_code == 204
    # Admin rights don't include reading other people's private documents.
    response = await api.delete(f"/api/documents/{other['id']}", headers=auth(ALICE))
    assert response.status_code == 404


async def test_unauthenticated_requests_are_401(api) -> None:
    assert (await api.get("/api/documents")).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert (await api.get("/api/documents", headers=bad)).status_code == 401


async def test_audit_log_records_uploads_and_is_admin_only(api, worker) -> None:
    await upload_ready(api, worker, BOB, "eng.md", HANDBOOK)
    assert (await api.get("/api/audit", headers=auth(BOB))).status_code == 403
    events = (await api.get("/api/audit?limit=200", headers=auth(ALICE))).json()
    actions = [e["action"] for e in events]
    assert "DOCUMENT_UPLOADED" in actions and "DOCUMENT_INGESTED" in actions


async def test_corrupt_files_fail_without_retries(
    api, worker: IngestionWorker, clean_database: DatabaseInfo
) -> None:
    response = await upload(api, BOB, "broken.pdf", b"%PDF-1.7 garbage that is not a pdf")
    assert response.status_code == 202
    await worker.run_until_idle()
    document = (await api.get(f"/api/documents/{response.json()['id']}", headers=auth(BOB))).json()
    assert document["status"] == "FAILED"
    assert document["error"]
    attempts = _job_attempts(clean_database, response.json()["id"])
    assert attempts == 1


def _job_attempts(db: DatabaseInfo, document_id: str) -> int:
    engine = create_engine(db.superuser_url)
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT attempts FROM eka.ingestion_jobs WHERE document_id = :id"),
            {"id": document_id},
        ).scalar_one()
    engine.dispose()
    return value
