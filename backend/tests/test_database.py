"""The database layer itself: schema, logins and row-level security (needs Docker).

These tests talk SQL directly, below the API, to prove the guarantees hold even for queries
the application doesn't make yet.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.db import Database
from app.models import Base
from tests.conftest import DatabaseInfo

pytestmark = pytest.mark.integration


def test_orm_models_match_the_migrated_schema(database: DatabaseInfo) -> None:
    """Like Hibernate's ddl-auto=validate: every mapped table and column exists in the DB."""
    engine = create_engine(database.superuser_url)
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        db_columns = {c["name"] for c in inspector.get_columns(table.name, schema="eka")}
        assert db_columns == {c.name for c in table.columns}, table.name
    engine.dispose()


async def test_api_login_is_subject_to_row_level_security(app_db: Database) -> None:
    await app_db.assert_row_level_security_applies()  # does not raise


async def test_worker_login_would_be_refused_as_api_login(worker_db: Database) -> None:
    with pytest.raises(RuntimeError, match="not subject to row-level security"):
        await worker_db.assert_row_level_security_applies()


async def _insert_document_as_owner(db: DatabaseInfo, principals: list[str]) -> uuid.UUID:
    """Creates a READY document with the given grants, bypassing the API (as superuser)."""
    document_id = uuid.uuid4()
    now = datetime.now(UTC)
    engine = create_engine(db.superuser_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eka.documents (id, title, filename, file_type, size_bytes, sha256,"
                " status, chunk_count, owner_sub, owner_name, created_at, updated_at)"
                " VALUES (:id, 'T', 't.md', 'markdown', 1, 'x', 'READY', 0, 'sub-owner',"
                " 'owner', :now, :now)"
            ),
            {"id": document_id, "now": now},
        )
        for principal in principals:
            conn.execute(
                text("INSERT INTO eka.document_grants VALUES (:id, :principal, 'sub-owner', :now)"),
                {"id": document_id, "principal": principal, "now": now},
            )
    engine.dispose()
    return document_id


async def _visible_ids(db: Database, principals: list[str] | None) -> set[uuid.UUID]:
    async with db.transaction() as session:
        if principals is not None:
            await session.execute(
                text("SELECT set_config('app.principals', :p, true)"),
                {"p": json.dumps(principals)},
            )
        rows = await session.execute(text("SELECT id FROM eka.documents"))
        return {row.id for row in rows}


async def test_rls_shows_only_granted_documents(
    app_db: Database, clean_database: DatabaseInfo
) -> None:
    hr = await _insert_document_as_owner(clean_database, ["group:/hr"])
    everyone = await _insert_document_as_owner(clean_database, ["everyone"])
    personal = await _insert_document_as_owner(clean_database, ["user:sub-erin"])

    assert await _visible_ids(app_db, ["group:/hr", "everyone"]) == {hr, everyone}
    assert await _visible_ids(app_db, ["user:sub-erin", "everyone"]) == {everyone, personal}
    assert await _visible_ids(app_db, ["group:/finance"]) == set()


async def test_rls_fails_closed_without_principals(
    app_db: Database, clean_database: DatabaseInfo
) -> None:
    await _insert_document_as_owner(clean_database, ["everyone"])
    # A query that forgot to set the user context sees nothing at all.
    assert await _visible_ids(app_db, None) == set()
    assert await _visible_ids(app_db, []) == set()


async def test_principals_do_not_leak_between_transactions(
    app_db: Database, clean_database: DatabaseInfo
) -> None:
    secret = await _insert_document_as_owner(clean_database, ["group:/hr"])
    async with app_db.engine.connect() as conn:
        # Same connection, two transactions: the setting is transaction-local.
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('app.principals', :p, true)"),
                {"p": json.dumps(["group:/hr"])},
            )
            assert (await conn.execute(text("SELECT count(*) FROM eka.documents"))).scalar() == 1
        async with conn.begin():
            assert (await conn.execute(text("SELECT count(*) FROM eka.documents"))).scalar() == 0
    assert secret


async def test_api_login_cannot_rewrite_the_audit_log(app_db: Database) -> None:
    async with app_db.engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await conn.execute(text("DELETE FROM eka.audit_events"))


def test_audit_log_is_append_only_even_for_its_owner(database: DatabaseInfo) -> None:
    engine = create_engine(database.settings.database_url("owner"))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eka.audit_events (id, occurred_at, actor_sub, actor_name, action)"
                " VALUES (:id, now(), 's', 'n', 'TEST')"
            ),
            {"id": uuid.uuid4()},
        )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("UPDATE eka.audit_events SET action = 'CHANGED'"))
    engine.dispose()


async def test_api_login_cannot_run_ddl(app_db: Database) -> None:
    async with app_db.engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="must be owner"):
            await conn.execute(text("ALTER TABLE eka.documents DISABLE ROW LEVEL SECURITY"))
