"""Settings, read from environment variables (and ../.env when run locally).

pydantic-settings maps each field to an env var of the same name, case-insensitive:
``oidc_issuer`` <- ``OIDC_ISSUER``. Secrets are ``SecretStr`` so they never show up in logs or
tracebacks by accident.
"""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database: one Postgres database, three logins (see infra/postgres/init/01-init.sh) ---
    db_host: str = "localhost"
    db_port: int = Field(
        default=25432, validation_alias=AliasChoices("DB_PORT", "POSTGRES_HOST_PORT")
    )
    db_name: str = "eka"
    eka_app_db_password: SecretStr = SecretStr("")  # API: every read goes through RLS
    eka_worker_db_password: SecretStr = SecretStr("")  # ingestion worker: BYPASSRLS
    eka_owner_db_password: SecretStr = SecretStr("")  # migrations only

    # --- Redis (rate limiting) ---
    redis_host: str = "localhost"
    redis_port: int = Field(
        default=26379, validation_alias=AliasChoices("REDIS_PORT", "REDIS_HOST_PORT")
    )
    redis_password: SecretStr = SecretStr("")

    # --- OpenID Connect (Keycloak) ---
    oidc_issuer: str = "http://localhost:8380/realms/eka"
    oidc_jwks_url: str = "http://localhost:8380/realms/eka/protocol/openid-connect/certs"
    oidc_audience: str = "eka-api"

    # --- Uploads ---
    max_upload_bytes: int = 20 * 1024 * 1024

    # --- Ingestion ---
    chunk_max_chars: int = 1200  # ~300 tokens; bge-small reads at most 512 tokens
    chunk_overlap_chars: int = 150
    # Prepend "document title > section" to each chunk before embedding. Off: on the demo eval
    # it made no measurable difference (hit@1 96% either way), so the simpler option wins.
    embed_heading_context: bool = False
    worker_poll_seconds: float = 5.0  # fallback when no NOTIFY arrives
    worker_lease_seconds: int = 300  # a crashed worker's job is retried after this
    worker_max_attempts: int = 3

    # --- Retrieval ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Where model files are cached. The Docker image bakes them in (no downloads at runtime).
    embedding_cache_dir: str | None = None
    retrieval_top_k: int = 6
    # Below this cosine similarity a passage counts as "not about the question". Leave unset to
    # use the value calibrated for the embedding model (DEFAULT_MIN_SCORES in embeddings.py).
    retrieval_min_score: float | None = None

    # --- Answers (Claude). Without an API key the assistant runs offline: no LLM calls. ---
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    # A streaming chat answer over a few passages: "low" keeps the pause before the first word
    # short. Change it only with evidence from an answer-quality eval (planned, see README).
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 120.0

    # --- Rate limits (per user, per minute) ---
    ask_rate_limit_per_minute: int = 10
    upload_rate_limit_per_minute: int = 20

    enable_docs: bool = True

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value().strip())

    def database_url(self, role: Literal["app", "worker", "owner"]) -> URL:
        """SQLAlchemy URL for one of the three database logins (psycopg 3 driver)."""
        password = {
            "app": self.eka_app_db_password,
            "worker": self.eka_worker_db_password,
            "owner": self.eka_owner_db_password,
        }[role]
        return URL.create(
            "postgresql+psycopg",
            username=f"eka_{role}",
            password=password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
