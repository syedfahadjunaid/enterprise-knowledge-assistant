"""FastAPI application: wiring, health checks and error handling.

``create_app`` takes optional dependencies so tests can swap in a local signing key, a fake
LLM or an in-memory rate limiter. That's dependency injection without a framework.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.answers.generators import (
    AnswerGenerator,
    ClaudeAnswerGenerator,
    OfflineAnswerGenerator,
)
from app.answers.service import AnswerService
from app.api import router
from app.config import Settings
from app.db import Database, create_engine
from app.documents import DocumentService
from app.problems import install_handlers
from app.ratelimit import RateLimiter, RedisRateLimiter
from app.retrieval.embeddings import Embedder, build_embedder, default_min_score
from app.security import TokenVerifier, jwks_key_resolver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("eka.api")


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    embedder: Embedder | None = None,
    generator: AnswerGenerator | None = None,
    verifier: TokenVerifier | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_db = db is None
        database = db or Database(create_engine(settings, "app"))
        await database.assert_row_level_security_applies()

        redis_client = None
        limiter = rate_limiter
        if limiter is None:
            redis_client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password.get_secret_value() or None,
                socket_timeout=2,
            )
            limiter = RedisRateLimiter(redis_client)

        emb = embedder or build_embedder(settings.embedding_model, settings.embedding_cache_dir)
        gen = generator or build_generator(settings)
        min_score = settings.retrieval_min_score
        if min_score is None:
            min_score = default_min_score(emb)
        log.info("Embedding model %s (cut-off %.2f), answers by %s", emb.name, min_score, gen.name)

        app.state.settings = settings
        app.state.db = database
        app.state.rate_limiter = limiter
        app.state.verifier = verifier or TokenVerifier(
            settings.oidc_issuer, settings.oidc_audience, jwks_key_resolver(settings.oidc_jwks_url)
        )
        app.state.documents = DocumentService(
            database,
            max_upload_bytes=settings.max_upload_bytes,
            max_attempts=settings.worker_max_attempts,
        )
        app.state.answers = AnswerService(
            database, emb, gen, top_k=settings.retrieval_top_k, min_score=min_score
        )
        yield
        if redis_client is not None:
            await redis_client.aclose()
        if owned_db:
            await database.engine.dispose()

    app = FastAPI(
        title="Enterprise Knowledge Assistant API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    install_handlers(app)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_security_headers)
    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "UP"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        try:
            await request.app.state.db.ping()
        except Exception:  # noqa: BLE001 - any failure means "not ready"
            log.exception("readiness check failed")
            return JSONResponse({"status": "DOWN"}, status.HTTP_503_SERVICE_UNAVAILABLE)
        return JSONResponse({"status": "UP"})

    return app


async def _security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.path.startswith("/api/"):
        # Answers and document lists are per-user: never let a shared cache keep them.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def build_generator(settings: Settings) -> AnswerGenerator:
    if not settings.llm_enabled:
        log.warning("ANTHROPIC_API_KEY not set: answering in offline mode (no LLM calls)")
        return OfflineAnswerGenerator()
    assert settings.anthropic_api_key is not None  # noqa: S101 - narrowed by llm_enabled
    return ClaudeAnswerGenerator(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.llm_model,
        effort=settings.llm_effort,
        max_tokens=settings.llm_max_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory app.main:app_factory``."""
    return create_app()
