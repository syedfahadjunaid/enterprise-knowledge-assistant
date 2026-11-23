"""Alembic environment: how migrations connect to the database.

Migrations always run as ``eka_owner``, the owner of schema ``eka``. The application logins
(``eka_app``, ``eka_worker``) cannot run DDL at all.

Tests pass an open connection in ``config.attributes["connection"]`` instead (see
tests/conftest.py), so they migrate the Testcontainers database the same way.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from app.config import Settings
from app.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)  # log to the console
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema="eka",  # keep Alembic's bookkeeping table in our schema too
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def main() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
        return
    engine = create_engine(Settings().database_url("owner"), poolclass=pool.NullPool)
    with engine.connect() as conn:
        run_migrations(conn)
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported; run against a database.")
main()
