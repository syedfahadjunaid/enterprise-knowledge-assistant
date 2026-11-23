"""Database access: async SQLAlchemy engines and the per-user transaction.

``Database.as_user(user)`` is the only way the API talks to the database. It opens a
transaction and stores the user's principals in transaction-local settings, which the
row-level security policies read (see migrations/versions/0001_initial.py). Because the
settings are local to the transaction (``set_config(..., true)``), a pooled connection can
never carry one user's permissions into another user's request.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.security import User

log = logging.getLogger(__name__)


def create_engine(settings: Settings, role: str) -> AsyncEngine:
    return create_async_engine(
        settings.database_url(role),  # type: ignore[arg-type]
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,  # survive a database restart without failing the next request
        connect_args={"application_name": f"eka-{role}"},
    )


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def as_user(self, user: User) -> AsyncIterator[AsyncSession]:
        """A transaction in which the database sees only what ``user`` may see.

        Commits when the block ends normally, rolls back if it raises.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.principals', :principals, true),"
                    " set_config('app.user_sub', :sub, true)"
                ),
                {"principals": json.dumps(list(user.principals)), "sub": user.sub},
            )
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A plain transaction, without user context (worker and health checks)."""
        async with self._sessions() as session, session.begin():
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def assert_row_level_security_applies(self) -> None:
        """Refuse to start if this login would skip row-level security.

        Superusers, roles with BYPASSRLS and the table owner are not subject to RLS. If the API
        were ever configured with such a login, every user could read every document, silently.
        """
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT r.rolsuper, r.rolbypassrls, t.tableowner = current_user"
                        " FROM pg_roles r, pg_tables t"
                        " WHERE r.rolname = current_user"
                        "   AND t.schemaname = 'eka' AND t.tablename = 'documents'"
                    )
                )
            ).one_or_none()
        if row is None:
            raise RuntimeError("Table eka.documents not found: run the migrations first")
        superuser, bypass_rls, owner = row
        if superuser or bypass_rls or owner:
            raise RuntimeError(
                "The API's database login is not subject to row-level security "
                f"(superuser={superuser}, bypassrls={bypass_rls}, owner={owner}). "
                "Use the eka_app login."
            )
        log.info("Row-level security applies to the API's database login")
