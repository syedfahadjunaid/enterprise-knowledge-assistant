# Enterprise Knowledge Assistant

Employees ask questions about internal documents and get streamed answers with citations. Each
answer uses **only the documents that person is allowed to read**. Permissions come from
Keycloak users and groups, and Postgres enforces them with row-level security on every query.

What this project demonstrates:

- **Access-controlled retrieval.** Document-level ACLs built from Keycloak groups, including
  subgroups, are enforced by Postgres row-level security inside every search. If the permission
  context is missing, queries return nothing (they fail closed).
- **An ingestion pipeline.** PDF, DOCX, Markdown, HTML and text files are parsed into
  section-aware chunks and embedded by a background worker. The worker uses a Postgres job queue
  (`FOR UPDATE SKIP LOCKED`, `LISTEN/NOTIFY`, leases, retries with backoff, fencing tokens).
- **LLM integration.** Claude answers through the official `anthropic` SDK, with native citations
  (`search_result` blocks), streaming to the browser over server-sent events, and server-side
  refusal fallback. An offline mode keeps working when no API key is set.
- **An audit log.** An append-only record of who was shown which documents, written in the same
  transaction as the search, before anything is streamed.
- **Evaluation.** A retrieval eval with confidence intervals decides defaults such as the
  "not found" cut-off and chunk heading context. A permission eval checks every eval question
  for every user against the real database.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, psycopg 3, Postgres 18 + pgvector
0.8, fastembed (`BAAI/bge-small-en-v1.5`, ONNX on CPU), Anthropic SDK (`claude-opus-5-5`), Redis,
Keycloak 26.8, React 19, TypeScript, Vite, Tailwind, Docker Compose and GitHub Actions.

## Architecture

```
Browser (React SPA, tokens in memory)
   |  1. sign in: authorization code + PKCE ----------------------> Keycloak (realm "eka")
   |  2. /api/* with the access token                                 users, groups, roles
   v
nginx (web) --/api--> FastAPI  "api"  (login eka_app: every read goes through RLS)
                        |  verify JWT -> principals = user:<sub>, group:/eng, group:/eng/platform, everyone
                        |  BEGIN; set_config('app.principals', ...); query; audit; COMMIT
                        |---> Redis (per-user rate limits)
                        |---> Claude API (only passages this user may read; optional)
                        v
                     Postgres 18 + pgvector  (schema "eka")
                        ^  documents, grants, files, chunks(vector 384), ingestion_jobs, audit_events
                        |  NOTIFY ingestion_jobs
                     "worker"  (login eka_worker, BYPASSRLS): parse -> chunk -> embed -> store
```

One Docker image runs as three containers: `migrate` (Alembic, login `eka_owner`), `api`
(uvicorn) and `worker`.

## How permissions work

1. **Identity comes only from the verified token.** The API checks the signature, expiry,
   issuer and audience of the Keycloak token, then builds the user's *principals*:
   `user:<sub>`, one `group:<path>` for every group *and every parent group* (members of
   `/engineering/platform` also count as `/engineering`), and `everyone`. Request bodies can't
   name a user, group or tenant (`extra="forbid"`).
2. **Grants.** Each document has rows in `document_grants`. The uploader always gets their own
   `user:` grant. Editors can share with groups they belong to (or their parent groups), and only
   `kb-admin` can share with `everyone`.
3. **The database filters every read.** Each API transaction starts with
   `set_config('app.principals', '[...]', true)`. Row-level security policies on `documents`,
   `document_grants`, `document_files` and `chunks` keep only documents that have a matching
   grant. The vector search in `app/retrieval/search.py` has **no permission WHERE clause of its
   own**. The database adds one to every query, including queries nobody has written yet.
4. **Fail closed.** Without the setting there are no principals, so queries return no rows. The
   setting is transaction-local, so a pooled connection never carries one user's permissions
   into another request. The API also refuses to start if its database login could skip RLS
   (superuser, `BYPASSRLS` or table owner).
5. **Status codes.** **404** if you may not see the document (the API never confirms that a hidden
   document exists), **403** if you may see it but not do this (for example, deleting someone
   else's document), **422** for invalid input (with `errors[]`), **429** when rate-limited.
6. **Admins are not superusers of content.** `kb-admin` reads the audit log and can share with
   everyone, but still sees only documents shared with them.

Group changes in Keycloak apply when the user's next access token is issued (within 5 minutes).

## Ingestion pipeline

Upload (`POST /api/documents`, multipart, max 20 MB) checks the extension **and** the content
(PDF magic bytes, DOCX zip structure with a zip-bomb limit, UTF-8 text). It then stores the
file, its grants and an `ingestion_jobs` row in **one transaction**, so a job exists exactly
when the upload committed. A trigger sends `NOTIFY` and the worker wakes up:

- **Claim:** `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`. Several workers
  never take the same job, and none waits for another.
- **Lease:** if a worker dies, its lease runs out and another worker retries the job. A worker
  saves results only if the job is still on its own attempt number (a fencing token), so a slow
  worker can't overwrite a newer attempt.
- **Retries:** corrupt or empty files fail at once with a readable error. Other errors are
  retried after 10 s, 20 s, 40 s and so on, up to 3 attempts.
- **Parsing:** Markdown (markdown-it), HTML (BeautifulSoup; drops scripts and navigation), DOCX
  (python-docx: headings, lists, tables) and PDF (pypdf, with page numbers). Everything becomes
  blocks tagged with a section path and page.
- **Chunking:** chunks never mix sections or PDF pages, so citations like "Travel policy > Hotels"
  or "page 3" are exact. Chunks are at most 1,200 characters, split between sentences, with a
  150-character overlap.
- **Embedding:** bge-small-en-v1.5 runs locally on the CPU, and the model is baked into the
  image. Document text never leaves the server for embedding.

## Answers

`POST /api/ask` embeds the question, runs the RLS-filtered vector search (top 6, HNSW index with
pgvector 0.8's iterative scan so filtering can't starve the results), writes the audit record
and **commits**. Only then does it stream server-sent events:

```
event: sources   data: [{"index":0,"document_title":"Company handbook","heading":"Remote work","text":"...","score":0.81}]
event: delta     data: {"text":"You can work from home up to three days per week."}
event: citation  data: {"source":0,"cited_text":"You may work from home up to three days per week."}
event: done      data: {"model":"claude-opus-5-5","stop_reason":"end_turn","no_llm_call":false}
```

- **Native citations.** Passages go to Claude as `search_result` content blocks with
  `citations: {enabled: true}`. The API returns `search_result_location` citations, so the UI
  links each sentence to its source without parsing "[1]" out of the text.
- **Model settings.** `claude-opus-5-5` with explicit `output_config.effort` (`low` for a
  streaming chat, to be re-checked by the planned answer eval) and server-side refusal fallback
  (`fallbacks: "default"`). The code checks `stop_reason`, and on a refusal the client replaces
  the streamed text.
- **"Not found" without an LLM call.** If no permitted passage scores above the cut-off, the
  answer says so and no model is called.
- **Offline mode.** Without `ANTHROPIC_API_KEY`, the assistant streams the opening of the best
  passages with the same citations. Permissions, audit and streaming all work, at no cost.
- **Prompt-injection containment.** Documents are data in their own content blocks, and the
  system prompt says they are not instructions. The real containment is structural: the model
  has no tools and only ever sees passages the asker could already read.
- **The UI renders everything as plain text** (no `dangerouslySetInnerHTML`). The nginx CSP
  allows no inline scripts or styles, and connections only to the app itself and Keycloak.

## Retrieval quality

`backend/evals/retrieval/run.py` runs the real parser → chunker → embedder over the demo
corpus and ranks chunks the way the pgvector query does. The dataset
(`datasets/demo.json`) has 50 answerable questions, everyday paraphrases with an expected
document and section, plus 8 questions no document answers. It was written before any results
were seen. Every number has a 95% bootstrap CI.

| Model (heading context off, cut-off 0.60) | hit@1 | section@1 | hit@3 | MRR | accuracy incl. "not found" |
|---|---|---|---|---|---|
| `BAAI/bge-small-en-v1.5` | **96.0%** [90, 100] | 94.0% | 98.0% | 97.5% | **91.4%** [84.5, 98.3] |
| `hashing-v1` (word-matching baseline) | 78.0% [66, 88] | 74.0% | 94.0% | 86.2% | n/a |

Decisions. The rules were written down before running bge-small:

- **Heading context off.** Rule: keep "document title > section" prepended to chunks before
  embedding only if the paired hit@1 gain has a CI above zero. Measured: +0.0% (both 96%), so
  the simpler option won.
- **"Not found" cut-off 0.60.** Rule: keep 0.55 (the HRMS value for the same model) unless
  another value beats it by more than 3 points. The sweep peaked at 0.600–0.625 with 91.4%,
  against 84.5% at 0.55. 0.60 is the lower of the tied values, so it hides fewer real answers.
  This is calibrated in-sample on only 8 unanswerable questions, so re-check it as the corpus
  grows.
- **Known misses (2 answerable):** "Who is allowed to see restricted data like salaries?" ranks
  the salary bands document first, and "What counts as a SEV1 incident?" ranks the runbook
  first. Three unanswerable questions (dress code, CEO, guest Wi-Fi) still score above the
  cut-off.
- **CI gate:** the build fails if bge-small's hit@1 drops below 85%.
- **Hybrid search and the reranker are not built yet.** They will be added behind flags and
  turned on only if this eval shows a gain whose CI is above zero (milestone 3).

The **permission eval** (`tests/test_permission_eval.py`) loads the same corpus into a real
Postgres through the API and worker. For each demo user it checks that the set of documents the
search can ever reach matches a hand-written table exactly. It then asks all 58 questions as all
5 users. Leaks: **0**.

## Run it

Requirements: Docker Desktop. For tests outside Docker: Python 3.12 and Node 24. Setting up a
new machine from a zip or a fresh clone is covered step by step, with troubleshooting, in
`AGENTS.md` ("Run it on a new machine"). If Windows blocks the scripts, run them as
`powershell -ExecutionPolicy Bypass -File .\scripts\seed-demo.ps1`.

```powershell
Copy-Item .env.example .env
docker compose up -d --build          # everything; first build downloads the embedding model
.\scripts\seed-demo.ps1                # uploads the demo documents as their owners
.\scripts\smoke-test.ps1               # signs in real users and checks permissions end to end
```

Open http://localhost:5380 and sign in as a demo user. The dev password is in
`infra/keycloak/eka-realm.json`.

| User | Role | Groups | Sees |
|---|---|---|---|
| `alice` | kb-admin, kb-editor | /hr | handbook, security, travel, **salary bands**; audit log |
| `bob` | kb-editor | /engineering/platform | handbook, security, travel, **on-call handbook, platform runbook** |
| `carol` | kb-editor | /finance | handbook, security, travel, **Q3 budget, procurement** |
| `dave` | (reader) | /engineering | handbook, security, travel, **on-call handbook** (not the platform runbook) |

Try asking each of them "What is the salary range for a senior engineer?".

To get real Claude answers, set `ANTHROPIC_API_KEY` in `.env` and run
`docker compose up -d api`. Each question with matching passages is then a paid API call.

Ports (unusual on purpose, all bound to 127.0.0.1): web **5380**, API **8300** (OpenAPI docs at
`/docs`), Keycloak **8380**, Postgres **25432**, Redis **26379**.

### Tests and checks

```powershell
cd backend; python -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest           # 98 tests; integration tests need Docker
.\.venv\Scripts\ruff.exe check .; .\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m evals.retrieval.run --compare heading-context --sweep
cd ..\web; npm ci; npm test; npm run lint; npm run build
```

Backend tests cover:

- token verification and principals;
- all four parsers (DOCX and PDF fixtures are generated in the tests);
- the chunking rules;
- the Claude stream handler, which the real SDK parses from a mocked transport, including
  citations, refusals and errors;
- rate limits (against a real Redis in Testcontainers);
- with Testcontainers: the database (schema vs ORM, RLS, fail-closed, transaction-local
  settings, append-only audit, no DDL for the API login), the documents and ask APIs, worker
  retries and leases, and the permission eval.

## Layout

| Path | What |
|---|---|
| `backend/app/` | FastAPI app: `security.py` (JWT, principals), `db.py` (per-user transactions), `documents.py`, `api.py`, `answers/`, `retrieval/`, `ingestion/` (file types, parsers, chunking, worker), `audit.py`, `ratelimit.py` |
| `backend/migrations/` | Alembic; `0001_initial.py` holds the tables, RLS policies and grants |
| `backend/evals/retrieval/` | Retrieval eval runner and dataset |
| `backend/tests/` | Unit and integration tests |
| `web/` | React SPA: Ask (streamed, cited), Documents (upload, share, delete), Audit log |
| `infra/` | Keycloak realm (users, groups, roles, clients) and the Postgres init script (logins) |
| `demo-docs/` | Demo corpus and `manifest.json` (owner and sharing per file) |
| `scripts/` | `seed-demo.ps1`, `smoke-test.ps1`, `get-token.ps1` |

## Roadmap

Milestone 1 (above) is done. The remaining items are listed in build order below. `AGENTS.md`
("Status") describes each one in enough detail to build it.

1. **Manage access and sources (milestone 2a):** re-share, revoke, rename, re-index, and an admin
   "Sources" page. Revoking access takes effect on the next query, with no re-indexing.
2. **Conversation history (milestone 2b):** follow-up questions are rewritten to stand alone
   and stored per user under the same RLS. History is private, even from admins.
3. **Answer-quality eval (milestone 3a, paid):** an LLM judge for correctness and groundedness,
   with citation coverage measured in code. It decides effort, top_k and prompt changes.
4. **Hybrid keyword + vector search (milestone 3b)**, then **5. a cross-encoder reranker (3c).**
   Each is turned on only if the eval shows a gain whose CI is above zero.
6. **Later:**
   - re-embedding after a model change;
   - original-file download;
   - object storage (S3/MinIO) instead of `bytea`;
   - OCR for scanned PDFs;
   - connectors (SharePoint, Confluence) that sync ACLs;
   - per-tenant isolation, observability and Helm.
