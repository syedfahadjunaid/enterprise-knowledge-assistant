"""Shared test fixtures.

* JWTs: we generate a throwaway RSA key and sign our own tokens, so tests exercise the real
  verification code (signature, issuer, audience, expiry) without running Keycloak.
* Database: one real Postgres + pgvector container per test session (Testcontainers), set up
  with the SAME init script as Docker Compose (infra/postgres/init/01-init.sh) and the same
  Alembic migrations. Tests connect with the same three logins as production, so row-level
  security is tested for real. Integration tests are skipped when Docker isn't available.
"""

import asyncio
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.answers.generators import AnswerGenerator, OfflineAnswerGenerator
from app.config import Settings
from app.db import Database
from app.ingestion.worker import IngestionWorker
from app.main import create_app
from app.ratelimit import InMemoryRateLimiter
from app.retrieval.embeddings import HashingEmbedder
from app.security import TokenVerifier

if sys.platform == "win32":
    # psycopg's async mode needs the selector event loop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
INIT_SCRIPTS = REPO_DIR / "infra" / "postgres" / "init"

ISSUER = "http://test-issuer/realms/eka"
AUDIENCE = "eka-api"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()

TEST_VERIFIER = TokenVerifier(ISSUER, AUDIENCE, key_resolver=lambda _token: _public_key)


# ------------------------------------------------------------------------------ users / tokens


@dataclass(frozen=True)
class Persona:
    sub: str
    username: str
    roles: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()


# Same shape as the demo realm (infra/keycloak/eka-realm.json), plus erin with no groups.
ALICE = Persona("sub-alice", "alice", ("kb-admin", "kb-editor"), ("/hr",))
BOB = Persona("sub-bob", "bob", ("kb-editor",), ("/engineering/platform",))
CAROL = Persona("sub-carol", "carol", ("kb-editor",), ("/finance",))
DAVE = Persona("sub-dave", "dave", (), ("/engineering",))
ERIN = Persona("sub-erin", "erin", (), ())
PERSONAS = (ALICE, BOB, CAROL, DAVE, ERIN)


def make_token(
    persona: Persona = BOB,
    *,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: int = 300,
    key: rsa.RSAPrivateKey = _private_key,
    algorithm: str = "RS256",
) -> str:
    now = int(time.time())
    claims = {
        "sub": persona.sub,
        "iss": issuer,
        "aud": [audience, "account"],
        "iat": now,
        "exp": now + expires_in,
        "preferred_username": persona.username,
        "name": persona.username.title(),
        "realm_access": {"roles": list(persona.roles)},
        "groups": list(persona.groups),
    }
    return jwt.encode(claims, key, algorithm=algorithm)


def auth(persona: Persona) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(persona)}"}


# ------------------------------------------------------------------------------ database


@dataclass(frozen=True)
class DatabaseInfo:
    settings: Settings
    superuser_url: str


@pytest.fixture(scope="session")
def database() -> Iterator[DatabaseInfo]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers layout
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]

    passwords = {
        "EKA_OWNER_DB_PASSWORD": "test-owner",
        "EKA_APP_DB_PASSWORD": "test-app",
        "EKA_WORKER_DB_PASSWORD": "test-worker",
        "KEYCLOAK_DB_PASSWORD": "test-keycloak",
    }
    container = PostgresContainer(
        "pgvector/pgvector:0.8.7-pg18",
        username="postgres",
        password="test-superuser",
        dbname="eka",
        driver="psycopg",
    )
    for name, value in passwords.items():
        container.with_env(name, value)
    container.with_volume_mapping(str(INIT_SCRIPTS), "/docker-entrypoint-initdb.d", "ro")
    try:
        container.start()
    except Exception as e:  # noqa: BLE001 - any Docker problem means "can't run here"
        pytest.skip(f"Docker is not available: {e}")

    settings = Settings(
        _env_file=None,
        db_host=container.get_container_host_ip(),
        db_port=int(container.get_exposed_port(5432)),
        db_name="eka",
        eka_owner_db_password=passwords["EKA_OWNER_DB_PASSWORD"],
        eka_app_db_password=passwords["EKA_APP_DB_PASSWORD"],
        eka_worker_db_password=passwords["EKA_WORKER_DB_PASSWORD"],
        embedding_model=HashingEmbedder.name,
    )
    _migrate(settings)
    try:
        yield DatabaseInfo(settings, container.get_connection_url())
    finally:
        container.stop()


def _migrate(settings: Settings) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    engine = create_sync_engine(settings.database_url("owner"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    engine.dispose()


@pytest.fixture
def clean_database(database: DatabaseInfo) -> DatabaseInfo:
    """Empties the document tables (the audit log is append-only and stays)."""
    engine = create_sync_engine(database.superuser_url)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE eka.documents CASCADE"))
    engine.dispose()
    return database


def _engine(settings: Settings, role: str) -> AsyncEngine:
    # NullPool: no connection outlives the test's event loop.
    return create_async_engine(settings.database_url(role), poolclass=NullPool)  # type: ignore[arg-type]


@pytest.fixture
async def app_db(clean_database: DatabaseInfo) -> AsyncIterator[Database]:
    engine = _engine(clean_database.settings, "app")
    yield Database(engine)
    await engine.dispose()


@pytest.fixture
async def worker_db(clean_database: DatabaseInfo) -> AsyncIterator[Database]:
    engine = _engine(clean_database.settings, "worker")
    yield Database(engine)
    await engine.dispose()


@pytest.fixture
def worker(worker_db: Database, clean_database: DatabaseInfo) -> IngestionWorker:
    return IngestionWorker(worker_db, HashingEmbedder(), clean_database.settings)


@pytest.fixture
def generator() -> AnswerGenerator:
    return OfflineAnswerGenerator()


@pytest.fixture
async def api(
    app_db: Database, clean_database: DatabaseInfo, generator: AnswerGenerator
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client for the real app, wired to the test database and fakes for the rest."""
    app = create_app(
        clean_database.settings,
        db=app_db,
        embedder=HashingEmbedder(),
        generator=generator,
        verifier=TEST_VERIFIER,
        rate_limiter=InMemoryRateLimiter(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client
