# AGENTS.md: guide for coding agents working on this repository

Enterprise knowledge assistant: employees ask questions about internal documents and get
streamed, cited answers that use only documents they may read. It is an enterprise-style
reference project, and the code doubles as learning material (the owner is learning), so keep
explanatory comments and idiomatic code. The README is the human-facing overview; this file is
the operating manual. When they disagree, check the code and fix the doc.

Contents: Status · Map · Run and verify · Run it on a new machine · Request flow ·
Invariants · Retrieval decisions · Conventions · Things that already bit us · Definition of
done · Learning guide.

## Status: what's built and what's left

**Done**

- **Milestone 1, permission-aware RAG core (2026-11-22):**
  - Keycloak groups become document grants, enforced by Postgres row-level security (RLS).
  - PDF, DOCX, Markdown, HTML and text uploads, ingested by a background worker.
  - Streamed answers with citations, from Claude or in offline mode.
  - An append-only audit log of the documents shown.
  - A retrieval eval (bge-small hit@1 96%) and a permission eval (0 leaks).
  - A React UI (Ask, Documents, Audit log) and CI.

**Still to build, in this order.** Each item builds on the one before. Apply the Invariants and
the Definition of done to every item, and write any new rule into this file *before* coding it.

1. **Manage access and sources (milestone 2a).**
   - **API:**
     - `GET /api/documents/{id}/grants`
     - `POST /api/documents/{id}/grants` with body `{"principal": "group:/x"}`
     - `DELETE /api/documents/{id}/grants/{principal}`
     - `PATCH /api/documents/{id}` with body `{"title": ...}`
     - `POST /api/documents/{id}/reindex`, which queues a new ingestion job
     - Rules: only the owner or a `kb-admin` may call these. Use the same share rules as upload
       (`User.share_targets`). The owner's own `user:` grant can never be removed. Error codes
       follow the existing pattern: 404 hidden, 403 not allowed, 422 bad principal.
   - **Database:** a new Alembic revision `0002` with:
     - a `DELETE` policy on `eka.document_grants` (owner, via `eka.owns_document`);
     - `UPDATE` permission plus a policy on `eka.documents` for the title;
     - the matching grants for `eka_app`.
     - For admins: also set `app.is_admin` in `Database.as_user` and add an `eka.is_admin()`
       helper. Decide and write down whether an admin may manage the grants of a document
       they can't read. Recommendation: yes for grant metadata, never for chunks or files.
   - **Audit:** record `GRANT_ADDED`, `GRANT_REMOVED`, `DOCUMENT_RENAMED` and
     `REINDEX_REQUESTED` in the same transaction as the change.
   - **Web:** a "Manage access" dialog on each document row, and an admin "Sources" page with
     every document, its owner, status and grants, failed ingestions and a retry button.
   - **Done when:** an integration test shows that revoking a group's grant removes that
     document from the very next answer (no re-indexing, because RLS reads grants live). Also
     add tests for each 403/404/422 case and extend `scripts/smoke-test.ps1`.
2. **Conversation history (milestone 2b).**
   - **Database:** new tables `conversations(id, owner_sub, title, created_at)` and
     `messages(id, conversation_id, role, text, cited_chunk_ids uuid[], created_at)`, with an
     RLS policy `owner_sub = current_setting('app.user_sub')`: history is private, even from
     admins.
   - **Asking:** `POST /api/ask` takes an optional `conversation_id`.
     - Rewrite each follow-up into a standalone search query: Claude at `effort: "low"`, or
       offline, the previous question plus the new one.
     - Retrieval still runs under RLS on every turn.
     - Send earlier turns to Claude as plain text **without `citations`**: API version
       2026-10-21 rejects user-turn text blocks that carry citations.
   - **Revoked sources:** decide and document what happens to stored answers whose sources
     were revoked later. Recommendation: keep the text and show those citations as "source no
     longer available".
   - **Web:** a conversation list and a thread view.
   - **Done when:** tests cover the query rewriting, per-user isolation and revoked sources.
3. **Answer-quality eval (milestone 3a).**
   - **Cost:** paid. It needs `ANTHROPIC_API_KEY` and the owner's OK before every run, and it
     doesn't run in CI.
   - **Design:** port the HRMS design (`services/ai-service/evals/answers/`):
     - run the real `AnswerService` on the demo questions;
     - judge `correct` (with the right document as reference) and `grounded` (against the
       shown passages only) with `claude-sonnet-5-5`, never the model under test;
     - measure `cited` in code;
     - tag each case as `answerable`, `retrieval-miss`, `unanswerable` or `no-llm-call`;
     - write failed calls to `errors.jsonl`;
     - test the harness offline.
   - **Then use it** to decide `llm_effort` (`low` today), `retrieval_top_k` and prompt changes.
4. **Hybrid search (milestone 3b).**
   - **Database:** a generated `tsvector` column with a GIN index on `eka.chunks` (new
     revision).
   - **Search:** the keyword search runs under the same RLS. Merge it with the vector ranking
     by reciprocal rank fusion in `app/retrieval/search.py`, behind a setting
     `retrieval_hybrid` (default off).
   - **Eval first:** add an `exact_terms` slice (policy numbers, product codes) to the eval
     dataset *before* measuring.
   - **Decision:** turn it on only if the paired gain has a 95% CI above zero, and name the
     slice the decision rests on.
5. **Reranker (milestone 3c).**
   - A fastembed `TextCrossEncoder` (`Xenova/ms-marco-MiniLM-L-6-v2`) re-orders the top ~20
     candidates. Bake the model into the image with a build arg.
   - Keep the embedding score as the "not found" gate: cross-encoder scores are on another
     scale.
   - Same eval rule as hybrid search; also measure the extra latency on a CPU.
6. **Later, roughly by value:**
   - **Re-embedding after an `embedding_model` change.** Today, chunks from the old model are
     simply not searched. This needs a worker job type plus a startup warning.
   - **Download of the original file:** `GET /api/documents/{id}/file` with
     `Content-Disposition: attachment` and `nosniff`. RLS already guards `document_files`.
   - **Object storage** (S3 or MinIO) behind a `BlobStore` protocol instead of `bytea`.
   - **OCR for scanned PDFs**, e.g. ocrmypdf in the worker image.
   - **Connectors** (SharePoint, Confluence, Google Drive) that import documents *and* sync
     their ACLs into grants.
   - **Multi-tenancy:** `tenant_id` in every table and in the RLS policies.
   - **Observability:** OpenTelemetry traces and structured logs.
   - **Deployment:** Helm/Kubernetes.

## Map

| Path | What | Stack |
|---|---|---|
| `backend/app/` | One codebase, two processes: the API (`main.py`, `api.py`) and the ingestion worker (`ingestion/worker.py`) | Python 3.12, FastAPI, SQLAlchemy 2 async, psycopg 3, pgvector, fastembed (ONNX), Anthropic SDK, Redis |
| `backend/migrations/` | Alembic migrations: tables, **row-level security policies**, grants | Alembic (raw SQL via `op.execute`) |
| `backend/evals/retrieval/` | Retrieval eval: runner + `datasets/demo.json` | Python, numpy |
| `backend/tests/` | Unit tests + Testcontainers integration tests (Postgres + pgvector) | pytest, pytest-asyncio |
| `web/` | SPA: Ask (SSE stream), Documents (upload/share/delete), Audit log | React 19, TypeScript, Vite 8, Tailwind 4, TanStack Query, React Router, oidc-client-ts |
| `infra/keycloak/eka-realm.json` | Realm `eka`: roles, groups, clients, demo users (dev password lives here) | Keycloak 26.8 |
| `infra/postgres/init/01-init.sh` | Logins `eka_owner` / `eka_app` / `eka_worker` (BYPASSRLS) / `keycloak`, schema `eka` | Postgres 18 + pgvector 0.8.7 |
| `demo-docs/` | Demo corpus (md, html, docx, pdf) + `manifest.json` (owner, sharing). DOCX/PDF come from `backend/scripts/make_demo_docs.py` | |
| `scripts/` | `seed-demo.ps1`, `smoke-test.ps1`, `get-token.ps1`, shared `lib.ps1` | PowerShell 5.1 |
| `docker-compose.yml` | Whole stack: postgres, keycloak, redis, migrate (one-shot), api, worker, web | |
| `.github/workflows/ci.yml` | Lint, tests, eval gate, Trivy, image builds | |

## Run and verify

The dev machine is Windows (PowerShell 5.1 and Git Bash) with Docker Desktop. The backend venv
is `backend/.venv`; there is no `uv`.

```powershell
Copy-Item .env.example .env                 # once; .env is git-ignored
docker compose up -d --build                # everything; web on http://localhost:5380
.\scripts\seed-demo.ps1                     # demo documents (idempotent)
.\scripts\smoke-test.ps1                    # end-to-end permission checks as real users
```

| Area | Check before you call a change done |
|---|---|
| Backend | In `backend/`: `.\.venv\Scripts\python.exe -m pytest` (integration tests need Docker; ~2 min), `.\.venv\Scripts\ruff.exe check .`, `.\.venv\Scripts\ruff.exe format --check .` |
| Retrieval | `python -m evals.retrieval.run` (`--compare heading-context`, `--sweep`, `--show-misses`, `--model hashing-v1`, `--fail-under 0.85`) |
| Web | In `web/`: `npm test`, `npm run lint`, `npm run build` (includes `tsc`) |
| Running stack | `docker compose up -d --build api worker` (or `web`), then `.\scripts\smoke-test.ps1` |

Ports (deliberately unusual: other local projects hold 5432, 6379, 5173 and the HRMS project
holds 5280, 8080, 8000, 8090, 8180, 15432, 16379; a payment-ledger project holds 8480, 8481,
35432): web **5380**, API **8300**, Keycloak **8380**,
Postgres **25432**, Redis **26379**. All bound to 127.0.0.1. Before adding a port, check
`Get-NetTCPConnection -State Listen -LocalPort <port>`.

Demo users (realm `eka`; the password is in the realm file, never repeat it in chat or docs):
`alice` kb-admin + kb-editor in /hr, `bob` kb-editor in /engineering/platform, `carol` kb-editor
in /finance, `dave` reader in /engineering.

## Run it on a new machine

These steps were checked from a **clean copy**: no `.env`, `node_modules`, `.venv`, `target`,
`dist`, caches or model caches. Every step below worked from that state.

**What has to be on the machine**

| Tool | Version | Needed for |
|---|---|---|
| Docker Desktop | A 2025 or newer release. Tested with Engine 29.9 / Compose 5.6; older versions are untested | Everything. All services, dependencies and the embedding model are built inside the images |
| Windows PowerShell 5.1 + `curl.exe` | Built into Windows 10/11 | `scripts/*.ps1` (seed, smoke test, tokens) |
| Python | 3.12 | Only for backend tests, lint and the retrieval eval outside Docker |
| Node.js | 24 (npm 11) | Only for web tests, lint and build, or `npm run dev` |
| Internet access | | The first image build (PyPI, npm, Docker Hub/Quay, and Hugging Face for the ~130 MB model) and the first eval run |

No JDK, Maven or global Python/Node packages are needed. The scripts are Windows-only: they use
Windows PowerShell and `curl.exe`. On macOS/Linux, use the web UI, or adapt `scripts/lib.ps1`
for `pwsh`. Docker and both test suites should work the same there, with `.venv/bin/...`
instead of `.venv\Scripts\...`, but only Windows has been tested.

**Ports must be free:** 5380 (web), 8300 (API), 8380 (Keycloak), 25432 (Postgres), 26379
(Redis). Check each one with `Get-NetTCPConnection -State Listen -LocalPort 5380` (no output
means free).
- Postgres and Redis can move: change `POSTGRES_HOST_PORT` and `REDIS_HOST_PORT` in `.env`.
- 5380 and 8380 are fixed. They appear in the Keycloak redirect URIs, the token issuer, the
  nginx CSP and `vite.config.ts`; change them all together.

**From the unzipped folder.** The first build and start take ~5-15 minutes: image builds,
then Keycloak's first start, which took 6 minutes on a busy machine.

```powershell
Copy-Item .env.example .env            # local dev values; every variable Compose reads is listed
docker compose up -d --build           # postgres, keycloak, redis, migrate (one-shot), api, worker, web
docker compose ps -a                   # api "healthy", migrate "Exited (0)"
.\scripts\seed-demo.ps1                # uploads demo-docs as their owners, waits until READY
.\scripts\smoke-test.ps1               # 13 permission checks as real Keycloak users
```

If Windows refuses to run the scripts ("running scripts is disabled on this system", or files
from a downloaded zip are blocked), run them as
`powershell -ExecutionPolicy Bypass -File .\scripts\seed-demo.ps1`. Or run `Unblock-File
.\scripts\*.ps1` once, plus `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` if the
policy is still Restricted.

| URL | What |
|---|---|
| http://localhost:5380 | The app. Sign in as `alice`, `bob`, `carol` or `dave`; the password is in `infra/keycloak/eka-realm.json` |
| http://localhost:8300/docs | API reference (OpenAPI). Call it with a token from `. .\scripts\get-token.ps1 bob` |
| http://localhost:8380/admin | Keycloak admin console: user `admin`, password `KEYCLOAK_ADMIN_PASSWORD` from `.env` |

To get real Claude answers (paid), put `ANTHROPIC_API_KEY=...` in `.env`, then run
`docker compose up -d api`.

**All test suites from a clean copy** (Docker must be running for the integration tests):

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest                                   # 98 tests, ~2 min (much longer on a busy machine)
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m evals.retrieval.run --fail-under 0.85    # downloads the model to backend\.cache once
cd ..\web
npm ci
npm test
npm run lint
npm run build
```

**Why a clean copy works**

- The backend image installs its Python dependencies and downloads the embedding model at
  build time (`backend/Dockerfile`). The web image runs `npm ci` from the committed
  `package-lock.json`.
- The demo DOCX and PDF are committed in `demo-docs/`; `backend/scripts/make_demo_docs.py`
  only rebuilds them.
- Nothing needed at runtime lives in an ignored folder: `.venv`, `node_modules`, `dist` and
  `.cache` are rebuilt by the steps above.
- `.env.example` lists every variable that Compose reads (`${...}`).
- Settings that are not in it (`LLM_MODEL`, `LLM_EFFORT`, `RETRIEVAL_TOP_K`, `CHUNK_MAX_CHARS`,
  ... in `backend/app/config.py`) have defaults. To override one in Docker, add it to that
  service's `environment:` in `docker-compose.yml`.
- `infra/postgres/init/01-init.sh` works with or without its executable bit. Postgres
  sources the file when it isn't executable, as after unzipping on macOS/Linux; that was
  tested too.

**Reset and troubleshooting**

- `docker compose down -v` deletes all data. The next `up` re-runs the Postgres init
  script, the migrations and the Keycloak realm import. Do this after changing passwords in
  `.env` (Postgres keeps the passwords from its first start) or after editing the realm file.
- Keycloak needs ~1 minute on first start. `api` and `web` wait for it, so `up` can look
  stuck for that long.
- The integration tests skip themselves with "Docker is not available" if Docker Desktop
  isn't running. Their first run pulls the `pgvector/pgvector:0.8.7-pg18` image.
- To work on the UI with hot reload, run `docker compose stop web`, then `cd web; npm run dev`
  (port 5380, proxies `/api` to 8300).

## Request flow

Browser → Keycloak (authorization code + PKCE, tokens in memory) → `web` (nginx) → `/api/*` →
`api` verifies the JWT (signature, issuer, audience `eka-api`) → builds principals → opens a
transaction with `set_config('app.principals', ...)` → Postgres applies RLS → (for `/api/ask`)
audit row committed → SSE stream: `sources`, `delta`*, `citation`*, `done` | `error`.
Uploads: file + grants + `ingestion_jobs` row in one transaction → trigger `NOTIFY` → `worker`
claims with `SKIP LOCKED`, parses, chunks, embeds, stores chunks, marks READY.

## Invariants: do not break these

**Access control**
- Identity, roles and groups come **only** from the verified JWT. Request models use
  `extra="forbid"`; never accept a user, group or tenant from a body or query string.
- Reads are filtered by **row-level security**, not by WHERE clauses in Python. Every API
  database access goes through `Database.as_user(user)` (`app/db.py`). Never add a code path
  that queries documents or chunks for a user without it, and never filter results afterwards
  in Python as a security measure.
- The API must connect as `eka_app` (not owner, not BYPASSRLS). `assert_row_level_security_applies`
  enforces this at startup; don't remove it. Only the worker uses `eka_worker`.
- New tables holding document content or metadata need RLS policies using
  `eka.readable_document_ids()` in the same migration, plus explicit grants for `eka_app` /
  `eka_worker`. Policies must fail closed when `app.principals` is unset.
- Group principals include parent groups (`User.group_principals`); sharing never flows from a
  parent to a subgroup. Only `kb-admin` may share with `everyone`; editors share only with
  their own (or parent) groups; only owners can add grants (DB policy `grants_insert`).
- Status codes: **404** if you may not see it, **403** if you may see it but not do this, **409**
  if the state forbids it, **422** for invalid input (RFC 9457 body with `errors[]`), **429**
  with `Retry-After`.

**Data integrity**
- Alembic migrations are immutable once applied: add a new revision, never edit an old one.
  `tests/test_database.py` checks the ORM models match the migrated schema.
- `audit_events` is append-only (privileges + trigger). Audit writes happen inside the
  transaction of the action they describe; for answers, the audit row commits **before** any
  passage is streamed.
- Upload = document + file + grants + job in one transaction (outbox). Worker results are saved
  only if the job is still on the same attempt (`_still_ours`): keep that fencing check.
- Each chunk stores its `embedding_model`; the search filters on it. The vector column is
  `vector(384)`: another dimension needs a migration.

**AI and web safety**
- Passages go to Claude as `search_result` blocks with citations enabled; the model has no
  tools. Keep `stop_reason` checks, explicit `output_config.effort`, and the server-side
  refusal fallback (`fallbacks="default"` + beta `server-side-fallback-2026-07-01`).
- Offline mode (no `ANTHROPIC_API_KEY`) must keep working; tests never call the real API.
  Paid runs need the owner's OK.
- LLM output and document text are rendered as plain text. Never use `dangerouslySetInnerHTML`.
- The web CSP (`web/nginx.conf`) allows no inline scripts/styles and connects only to `self` +
  Keycloak. `/api/ask` must stay unbuffered in nginx (SSE).
- SPA tokens stay in memory (`InMemoryWebStorage`); post-login redirects go through
  `safeReturnTo`.
- Secrets only in `.env` (git-ignored). Compose uses `${VAR:?...}` so nothing has a default
  password.

## Retrieval changes are decided by the eval

- Defaults: `BAAI/bge-small-en-v1.5`, "not found" cut-off **0.60** (`DEFAULT_MIN_SCORES` in
  `app/retrieval/embeddings.py`), heading context **off** (`embed_heading_context`), chunks 1,200
  chars with 150 overlap, top_k 6. Numbers and reasoning are in the README.
- Cut-offs are per model; never reuse one model's cut-off for another.
- Rule: turn a retrieval feature on only if its paired improvement has a 95% CI above zero on
  the eval. Write the decision rule down **before** running the comparison.
- Don't add questions after seeing results to make a feature look good. A new kind of question
  becomes a new slice/dataset with its rule written down first. `tests/test_eval_dataset.py`
  keeps the dataset consistent with the corpus.
- CI fails if bge-small hit@1 drops below 85% (currently 96%).
- Changing the demo corpus changes the eval: rerun it and update the README numbers.

## Conventions

**Python (backend)**
- Ruff (line length 100, includes bandit `S` and `ASYNC` rules); type hints; `Protocol`
  interfaces (`Embedder`, `AnswerGenerator`, `RateLimiter`); `create_app(settings, db=...,
  embedder=..., generator=..., verifier=..., rate_limiter=...)` for dependency injection.
- Async everywhere in the API; CPU-bound work (embedding, parsing) via `asyncio.to_thread`.
- Thin routes in `api.py`, rules in services (`documents.py`, `answers/service.py`), errors as
  `ProblemError` (`problems.py`).
- Tests: JWTs signed with a local RSA key (`tests/conftest.py` personas mirror the realm);
  integration tests use the session-wide Testcontainers DB set up with the real init script and
  migrations, and `clean_database` truncates documents per test. The audit log is never
  truncated, so tests must not assume it is empty.
- The Anthropic SDK uses `httpx2`, not `httpx`: mock it with `httpx2.MockTransport`.

**Web**
- All HTTP goes through `apiRequest`/`apiFetch` (`api/client.ts`) and the hooks in
  `api/hooks.ts`; show server field errors with `ApiError.messageFor(field)`.
- The SSE stream is read with fetch + `SseParser` (`lib/sse.ts`); answer state is the pure
  reducer in `lib/answer.ts` (unit-tested).
- Role-based UI is cosmetic; the server enforces everything.
- Named exports, TypeScript strict, Tailwind classes, Vitest + Testing Library.

## Things that already bit us

- **Windows + psycopg async** needs the selector event loop: `tests/conftest.py` and the worker's
  `__main__` set `WindowsSelectorEventLoopPolicy` on win32.
- **PowerShell 5.1** passes a JSON array from `Invoke-RestMethod` down the pipeline as ONE
  object, so `@(Invoke-RestMethod ...)` has Count 1. Use `Get-ApiList` from `scripts/lib.ps1`.
  PS 5.1 has no `Invoke-RestMethod -Form`; uploads use `curl.exe` with `--form-string` for text.
- **Keycloak realm import runs only on first start** (persisted in Postgres). Change the running
  realm with `kcadm.sh` *and* update `eka-realm.json`; pipe scripts with Git Bash (PowerShell adds
  a BOM/CRLF).
- **RLS and INSERT ... RETURNING**: a new document is not readable until its grants exist, so
  inserts in `documents.py` use plain `insert()` without RETURNING, and the ORM uses
  client-side ids/timestamps.
- **Rate limits**: 10 questions and 20 uploads per user per minute (Redis, fails open).
- `testcontainers.postgres` / `.redis` are deprecated; import from `testcontainers.community.*`.
- **Dependency drift**: dependencies use `>=` ranges, so a clean install gets the newest
  versions. redis-py 8.2 broke `fakeredis` (an in-process Redis imitation), which is why the
  Redis limiter is tested against a real Redis container. Prefer real services in
  Testcontainers over imitation libraries. If an upstream release breaks the build, pin it with
  an upper bound and a comment explaining why.
- **Slow first Keycloak start**: on a busy machine the first start (optimized build + realm
  import) took over 6 minutes. That is why its healthcheck has `start_period: 300s`. If
  `docker compose up` still reports keycloak unhealthy, wait and run it again.

## Definition of done

1. Tests and linters pass for every part you touched (commands above).
2. Retrieval changes: eval re-run, numbers and decision recorded in the README.
3. Schema changes: a new Alembic revision with RLS policies and grants where needed.
4. The running stack is rebuilt and `scripts/smoke-test.ps1` passes.
5. README and this file updated if behaviour, commands, ports or decisions changed.
6. Don't commit or push unless the owner asks.

## Learning guide

This guide is for someone who knows some Java and is new to Python, RAG and LLM integration. Read
it next to the code: every concept names the files where it happens. The comments in those
files explain the *why*.

### Python for Java developers

| Here (Python) | Closest Java / Spring idea |
|---|---|
| `async def` + `await` (asyncio) | Virtual threads or reactive code. One thread runs many requests; `await` is where a request waits for I/O and lets others run |
| `asyncio.to_thread(fn, ...)` | Running blocking work on an executor so the event loop isn't frozen |
| FastAPI route function `@router.get(...)` (`app/api.py`) | A `@RestController` method |
| `Depends(...)`, `create_app(..., db=..., generator=...)` (`app/main.py`) | Constructor injection / `@Bean` wiring. Tests pass fakes instead of using Spring's context |
| `pydantic.BaseModel` (`app/schemas.py`) | A record plus Bean Validation (`@Size`, `@NotBlank`) |
| `pydantic-settings` `Settings` (`app/config.py`) | `@ConfigurationProperties` bound from environment variables |
| `typing.Protocol` (`Embedder`, `AnswerGenerator`, `RateLimiter`) | An interface, but *structural*: any class with matching methods fits, with no `implements` |
| `@dataclass(frozen=True)` | A `record` |
| `with` / `async with` (context managers) | try-with-resources |
| Decorators (`@router.post`, `@pytest.fixture`) | Annotations, except that a decorator is code that runs and wraps the function |
| SQLAlchemy 2 ORM + Core (`app/models.py`) | JPA/Hibernate entities + a query builder |
| Alembic (`migrations/`) | Flyway |
| pytest fixtures (`tests/conftest.py`) | JUnit 5 `@BeforeEach` / extensions, injected by parameter name |
| Ruff | Checkstyle + SpotBugs + a formatter, in one fast tool |

### Concepts, in suggested reading order

1. **The big picture.** One FastAPI codebase runs as two processes, the API and the worker, plus
   a one-shot migration container. Postgres is both the database *and* the vector store.
   Read: `README.md` → `docker-compose.yml` → `backend/Dockerfile`.
2. **Configuration and secrets.** Every setting is an environment variable with a typed
   default. Secrets are `SecretStr`, so they can't end up in logs, and they live only in
   `.env`. Compose's `${VAR:?}` refuses to start without them.
   Read: `.env.example` → `backend/app/config.py`.
3. **Authentication with OpenID Connect.**
   - The browser signs in with the *authorization code flow + PKCE*. A public client (the SPA)
     proves it started the login with a one-time secret, so no client secret is shipped to
     browsers.
   - The API receives a **JWT** and checks its signature against Keycloak's public keys
     (**JWKS**), plus expiry, issuer and audience. It never trusts claims it hasn't verified.
   - Roles come from `realm_access.roles`, groups from the `groups` claim.

   Read: `infra/keycloak/eka-realm.json` (clients, groups, mappers) → `web/src/auth/userManager.ts`
   → `backend/app/security.py` → `tests/test_security.py`.
4. **Authorization: roles vs ACLs.**
   - **Roles** (`kb-admin`, `kb-editor`) say what kind of action you may do.
   - **ACLs** (`document_grants`) say which *document* you may read.
   - A user's *principals* (`user:<sub>`, `group:/eng`, `everyone`) are matched against
     grants. Subgroup members inherit their parent groups' access, never the other way round.
   - The API answers 404 for documents you can't see, so it doesn't reveal that they exist.

   Read: `security.py` (`User.principals`) → `app/documents.py` → `tests/test_documents_api.py`.
5. **Postgres row-level security, the heart of the project.**
   - A *policy* is a WHERE clause the database adds to every query on a table.
   - The API stores the caller's principals with `set_config(..., true)`, which is local to
     the transaction, so it can't leak between pooled connections.
   - A `SECURITY DEFINER` function reads the grants table without recursing into its own
     policy.
   - If the setting is missing, the policies match nothing: they *fail closed*.
   - Least privilege: three logins, and only the worker may bypass RLS.

   Read: `infra/postgres/init/01-init.sh` → `migrations/versions/0001_initial.py` →
   `app/db.py` → `app/retrieval/search.py` (no permission filter!) → `tests/test_database.py`.
6. **The RAG pipeline (retrieval-augmented generation).** The model answers from passages we
   retrieve, not from its memory.
   - **Parsing** turns each format into text blocks with their section and page.
   - **Chunking** cuts them into passages that fit the embedding model and keep exact
     citations.
   - **Embedding** turns text into a 384-number vector whose direction encodes meaning.
     Similar meaning means a small angle between vectors; *cosine similarity* measures it.
   - **Vector search** finds the nearest chunks. An **HNSW index** makes that fast, with
     approximate results; *iterative scans* keep searching when a filter removes the first
     hits.
   - A **cut-off** turns "nothing relevant" into an honest "not found".

   Read: `app/ingestion/filetypes.py` → `parsers.py` → `chunking.py` →
   `app/retrieval/embeddings.py` → `search.py` → `tests/test_parsing.py`, `test_chunking.py`.
7. **Background jobs on Postgres.**
   - **Transactional outbox:** the job row commits together with the upload, so a job exists
     exactly when the upload does.
   - `LISTEN/NOTIFY` wakes the worker; polling is the fallback.
   - `FOR UPDATE SKIP LOCKED` lets many workers claim different jobs without waiting.
   - A **lease** (`locked_until`) recovers jobs from crashed workers.
   - A **fencing token** (the attempt number) stops a late worker from overwriting newer
     results.
   - **Exponential backoff** spaces out retries. Errors are either *permanent* (a corrupt
     file: fail now) or *transient* (retry).

   Read: `app/documents.py` (`upload`) → `app/ingestion/worker.py` → `tests/test_worker.py`.
8. **Calling an LLM safely.**
   - The official `anthropic` SDK, with passages sent as `search_result` blocks so Claude
     returns **native citations**.
   - Settings: explicit `effort`, `max_tokens`, and **refusal fallbacks**.
   - Always check `stop_reason` (`end_turn`, `max_tokens`, `refusal`) before trusting the
     content.
   - **Prompt injection:** a document may contain "ignore your instructions". It is contained
     because documents are data blocks, the model has no tools, and it only sees passages
     the user could read anyway.
   - **Offline mode** keeps the product usable, and testable, without a key.

   Read: `app/answers/prompts.py` → `generators.py` → `tests/test_generators.py` (the real
   SDK parsing a mocked stream).
9. **Streaming to the browser.**
   - **Server-sent events** are a simple text protocol (`event:` / `data:` lines, blank line
     between events) over one HTTP response.
   - The API commits the audit row first, then streams.
   - nginx must not buffer the stream.
   - The browser reads it with `fetch` and a stream reader, because `EventSource` can't send
     an `Authorization` header. A *reducer* turns events into UI state.

   Read: `app/answers/service.py` → `app/api.py` (`ask`) → `web/nginx.conf` →
   `web/src/lib/sse.ts` → `lib/answer.ts` → `lib/askStream.ts` → `pages/AskPage.tsx` →
   `components/AnswerView.tsx`.
10. **Audit, rate limits and errors.**
    - The audit log is *append-only*: privileges and a trigger stop changes. It is written in
      the same transaction as the action, so it can't miss or invent an event.
    - Rate limits use fixed one-minute windows in Redis (`INCR` + `EXPIRE`). They fail open,
      because they protect cost, not access.
    - Errors follow RFC 9457 *problem details*.

    Read: `app/audit.py` → `app/ratelimit.py` → `app/problems.py`.
11. **Evaluation: measure before you decide.**
    - A labelled dataset, ranking metrics (hit@1, hit@3, MRR) and accuracy including
      "not found".
    - **Bootstrap confidence intervals**: resample the questions thousands of times to see
      how much a number could move. With 58 questions, intervals are wide.
    - **Paired comparisons**: the same questions with feature on and off.
    - **Pre-registered rules**: write the rule before looking at results.
    - The permission eval treats security like quality: count the leaks, and expect 0.

    Read: `backend/evals/retrieval/run.py` → `datasets/demo.json` → README "Retrieval quality"
    → `tests/test_permission_eval.py`.
12. **Testing strategy.**
    - Pure unit tests for logic.
    - **Testcontainers** for a real Postgres with the real init script and migrations (no
      database mocks for security-critical SQL).
    - Fakes injected through Protocols.
    - Mocked HTTP transports for the LLM.
    - Vitest and Testing Library for the UI, including "renders HTML as text, never as
      markup".

    Read: `tests/conftest.py` → `tests/helpers.py` → any test file → `web/src/**/*.test.ts(x)`.
13. **Web security basics.**
    - A strict **Content-Security-Policy**: no inline scripts or styles, and connections
      only to the app and Keycloak.
    - Tokens in memory, not localStorage.
    - An open-redirect guard (`safeReturnTo`).
    - Plain-text rendering of model output.

    Read: `web/nginx.conf` → `web/src/auth/userManager.ts` → `components/AnswerView.test.tsx`.

### Glossary

- **ACL (access-control list):** per-resource list of who may access it; here, rows in
  `document_grants`.
- **Principal:** an identity a grant can name: a user (`user:<sub>`), a group
  (`group:/path`) or `everyone`.
- **RBAC:** role-based access control (`kb-admin`, `kb-editor`).
- **RLS (row-level security):** Postgres policies that filter rows per query.
  - **Policy:** one such rule, for a table and a command (SELECT, INSERT, DELETE).
  - **`SECURITY DEFINER`:** a function that runs with its owner's rights, not the caller's.
  - **`BYPASSRLS`:** a role attribute that skips RLS; only the worker has it.
- **Fail closed:** when context is missing, deny everything rather than allow everything.
- **Least privilege:** each component gets only the rights it needs (three database logins).
- **OIDC / OAuth 2:** standards for signing in and for delegated access with tokens.
  - **Authorization code + PKCE:** the browser sign-in flow; PKCE protects public clients.
  - **JWT:** a signed JSON token; claims include `sub`, `iss`, `aud`, `exp`, roles and groups.
  - **JWKS:** the identity provider's published public keys, used to check JWT signatures.
  - **Audience (`aud`):** which API a token is meant for (`eka-api`); others must reject it.
- **RAG:** retrieval-augmented generation; answer from retrieved passages.
  - **Chunk / passage:** a piece of a document small enough to embed and cite.
  - **Embedding:** a vector representing a text's meaning.
  - **Cosine similarity:** angle-based similarity between vectors, 1 = same direction.
  - **HNSW:** a graph-based approximate nearest-neighbour index (pgvector).
  - **Top-k:** how many passages the search returns (6).
  - **Cut-off (threshold):** the minimum similarity to count as relevant (0.60 for
    bge-small).
- **Eval metrics:**
  - **hit@k:** the right document is in the top k results.
  - **MRR:** mean of 1/rank of the right document.
  - **Bootstrap CI:** a range of plausible values, from resampling the dataset.
- **Citation:** a pointer from answer text to the passage that supports it.
- **Prompt injection:** text in the input that tries to give the model new instructions.
- **Effort:** a Claude setting for how much it thinks before answering (`low` … `max`).
- **`stop_reason`:** why the model stopped (`end_turn`, `max_tokens`, `refusal`).
- **Refusal fallback:** the API re-runs a declined request on another model.
- **SSE (server-sent events):** a one-way stream of text events over HTTP.
- **Job queue terms:**
  - **Transactional outbox:** write the "do this later" record in the same transaction as
    the change.
  - **`SKIP LOCKED`:** skip rows another transaction has locked; the basis of a Postgres job
    queue.
  - **Lease:** a time-limited claim on a job.
  - **Fencing token:** a number that lets the system reject work from an outdated claim.
  - **Exponential backoff:** retry delays that double each time.
- **Migration:** a versioned, never-edited schema change (Alembic here, Flyway in Java).
- **Testcontainers:** starts real dependencies (Postgres) in Docker for tests.
- **CSP (Content-Security-Policy):** a browser header that limits where scripts, styles and
  connections may come from.

### Exercises

Each exercise touches a different concept. Follow the Definition of done.

1. **Download the original file** (API + RLS, small).
   - Add `GET /api/documents/{id}/file`: return the bytes from `document_files` with
     `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.
   - Write integration tests: the owner and a group member get 200; a user outside the
     group gets **404** (not 403); an HTML file is never served as `text/html`.
   - Question to answer: why don't you need a permission check in Python?
2. **Revoke a share** (migration + RLS, medium). Item 1 of the build list, cut down:
   - Add migration `0002` with a `DELETE` policy on `eka.document_grants` for owners, and
     the endpoint `DELETE /api/documents/{id}/grants/{principal}`.
   - Test: bob shares the on-call handbook with `/engineering`, dave gets an answer from
     it, bob revokes, dave's next question shows no passage from it. No re-indexing needed.
3. **Chunk-size experiment** (evaluation, small).
   - Before running anything, write in this file a rule such as "switch to 600-character
     chunks only if the paired hit@1 gain has a 95% CI above zero".
   - Run `python -m evals.retrieval.run --max-chars 600 --overlap 75` against the default,
     paired: add a `--compare chunk-size` option modelled on `--compare heading-context`.
   - Record the numbers and the decision in the README, whatever they are.
4. **Two workers at once** (concurrency, medium).
   - In `tests/test_worker.py`, upload five documents and run two `IngestionWorker`s
     concurrently with `asyncio.gather(w1.run_until_idle(), w2.run_until_idle())`.
   - Assert: every document is READY, every job has `attempts == 1`, and the two workers
     processed 5 jobs together.
   - Then explain in a comment which SQL clause makes this safe.
5. **Bridge to Java** (Java practice, small).
   - In a scratch Maven project (or the HRMS core-service tests), port `User.principals` and
     `User.share_targets` from `app/security.py` to a Java 25 `record` with a static factory
     that reads the JWT claims map.
   - Write JUnit 5 tests mirroring `tests/test_security.py` (subgroups get parent groups,
     only admins may share with everyone).
   - Compare the two: which language made the parent-group expansion clearer, and why?
