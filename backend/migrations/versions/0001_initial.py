"""Initial schema: documents, grants, chunks, ingestion jobs, audit log, row-level security.

Revision ID: 0001
Revises:
Create Date: 2026-11-22

How access control works in the database
----------------------------------------
Each API transaction first stores the caller's principals in a transaction-local setting:

    SELECT set_config('app.principals', '["user:<sub>", "group:/hr", "everyone"]', true)

Row-level security (RLS) policies then filter every read of documents, grants, files and chunks
to documents that have a grant for one of those principals. The vector search in
``app/retrieval/search.py`` has no permission WHERE clause of its own: the database adds it to
every query, including queries nobody has written yet. If the setting is missing, the policies
match nothing (fail closed).

Migrations are immutable once applied: change the schema with a new revision.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    # ------------------------------------------------------------------ tables
    """
    CREATE TABLE eka.documents (
        id          uuid PRIMARY KEY,
        title       text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
        filename    text NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
        file_type   varchar(16) NOT NULL
                    CHECK (file_type IN ('pdf', 'docx', 'markdown', 'html', 'text')),
        size_bytes  integer NOT NULL CHECK (size_bytes > 0),
        sha256      varchar(64) NOT NULL,
        status      varchar(16) NOT NULL
                    CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED')),
        error       text,
        chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
        owner_sub   text NOT NULL,
        owner_name  text NOT NULL,
        created_at  timestamptz NOT NULL,
        updated_at  timestamptz NOT NULL
    )
    """,
    "CREATE INDEX documents_owner_idx ON eka.documents (owner_sub)",
    """
    CREATE TABLE eka.document_files (
        document_id uuid PRIMARY KEY REFERENCES eka.documents (id) ON DELETE CASCADE,
        data        bytea NOT NULL
    )
    """,
    """
    CREATE TABLE eka.document_grants (
        document_id uuid NOT NULL REFERENCES eka.documents (id) ON DELETE CASCADE,
        principal   text NOT NULL CHECK (
                        principal = 'everyone'
                        OR principal ~ '^user:.+$'
                        OR principal ~ '^group:/.+$'),
        granted_by  text NOT NULL,
        created_at  timestamptz NOT NULL,
        PRIMARY KEY (document_id, principal)
    )
    """,
    # The RLS helper looks grants up by principal, so index that direction too.
    "CREATE INDEX document_grants_principal_idx ON eka.document_grants (principal, document_id)",
    """
    CREATE TABLE eka.chunks (
        id              uuid PRIMARY KEY,
        document_id     uuid NOT NULL REFERENCES eka.documents (id) ON DELETE CASCADE,
        ordinal         integer NOT NULL CHECK (ordinal >= 0),
        text            text NOT NULL,
        heading         text,
        page            integer CHECK (page >= 1),
        embedding_model text NOT NULL,
        embedding       vector(384) NOT NULL,
        UNIQUE (document_id, ordinal)
    )
    """,
    # Approximate nearest-neighbour index for cosine distance (the <=> operator).
    "CREATE INDEX chunks_embedding_hnsw_idx ON eka.chunks USING hnsw (embedding vector_cosine_ops)",
    """
    CREATE TABLE eka.ingestion_jobs (
        id           uuid PRIMARY KEY,
        document_id  uuid NOT NULL REFERENCES eka.documents (id) ON DELETE CASCADE,
        status       varchar(16) NOT NULL
                     CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
        attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        max_attempts integer NOT NULL CHECK (max_attempts > 0),
        run_after    timestamptz NOT NULL,
        locked_until timestamptz,
        last_error   text,
        created_at   timestamptz NOT NULL,
        updated_at   timestamptz NOT NULL
    )
    """,
    """
    CREATE INDEX ingestion_jobs_due_idx ON eka.ingestion_jobs (run_after)
        WHERE status IN ('QUEUED', 'RUNNING')
    """,
    """
    CREATE TABLE eka.audit_events (
        id           uuid PRIMARY KEY,
        occurred_at  timestamptz NOT NULL,
        actor_sub    text NOT NULL,
        actor_name   text NOT NULL,
        action       text NOT NULL,
        document_ids uuid[] NOT NULL DEFAULT '{}',
        details      jsonb NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX audit_events_time_idx ON eka.audit_events (occurred_at DESC)",
    # "Who was shown document X?" is a containment query on the array.
    "CREATE INDEX audit_events_documents_idx ON eka.audit_events USING gin (document_ids)",
    # ------------------------------------------------------------------ append-only audit log
    # Privileges already forbid UPDATE/DELETE for the application roles; the trigger also stops
    # the schema owner, so changing history needs a deliberate migration.
    """
    CREATE FUNCTION eka.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' is append-only';
    END
    $$
    """,
    """
    CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON eka.audit_events
        FOR EACH ROW EXECUTE FUNCTION eka.reject_change()
    """,
    """
    CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON eka.audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION eka.reject_change()
    """,
    # ------------------------------------------------------------------ wake the worker
    # NOTIFY is delivered when the inserting transaction commits, so the worker never sees a
    # job whose document isn't there yet.
    """
    CREATE FUNCTION eka.notify_ingestion_job() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_notify('ingestion_jobs', '');
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE TRIGGER ingestion_jobs_notify AFTER INSERT ON eka.ingestion_jobs
        FOR EACH STATEMENT EXECUTE FUNCTION eka.notify_ingestion_job()
    """,
    # ------------------------------------------------------------------ row-level security
    # The caller's principals, from the transaction-local setting the API writes. Missing or
    # empty setting -> no principals -> no rows.
    """
    CREATE FUNCTION eka.current_principals() RETURNS text[]
        LANGUAGE sql STABLE AS $$
        SELECT coalesce(array_agg(p), '{}'::text[])
        FROM jsonb_array_elements_text(
            coalesce(nullif(current_setting('app.principals', true), ''), '[]')::jsonb) AS p
    $$
    """,
    # SECURITY DEFINER: runs as the schema owner, so it can read the grants table without
    # triggering the grants table's own policy (which would recurse). It returns only ids.
    """
    CREATE FUNCTION eka.readable_document_ids() RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = eka, pg_temp AS $$
        SELECT DISTINCT g.document_id
        FROM eka.document_grants g
        WHERE g.principal = ANY (eka.current_principals())
    $$
    """,
    """
    CREATE FUNCTION eka.owns_document(doc uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = eka, pg_temp AS $$
        SELECT EXISTS (
            SELECT 1 FROM eka.documents d
            WHERE d.id = doc
              AND d.owner_sub = nullif(current_setting('app.user_sub', true), ''))
    $$
    """,
    "REVOKE ALL ON FUNCTION eka.current_principals() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION eka.readable_document_ids() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION eka.owns_document(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION eka.readable_document_ids() TO eka_app",
    "GRANT EXECUTE ON FUNCTION eka.owns_document(uuid) TO eka_app",
    # Reads: only documents you have a grant for. "IN (SELECT ...)" makes Postgres compute the
    # readable set once per query and hash it, instead of calling the function per row.
    "ALTER TABLE eka.documents ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY documents_read ON eka.documents FOR SELECT
        USING (id IN (SELECT eka.readable_document_ids()))
    """,
    """
    CREATE POLICY documents_insert ON eka.documents FOR INSERT
        WITH CHECK (owner_sub = nullif(current_setting('app.user_sub', true), ''))
    """,
    """
    CREATE POLICY documents_delete ON eka.documents FOR DELETE
        USING (id IN (SELECT eka.readable_document_ids()))
    """,
    "ALTER TABLE eka.document_grants ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY grants_read ON eka.document_grants FOR SELECT
        USING (document_id IN (SELECT eka.readable_document_ids()))
    """,
    # Only a document's owner can share it.
    """
    CREATE POLICY grants_insert ON eka.document_grants FOR INSERT
        WITH CHECK (eka.owns_document(document_id))
    """,
    "ALTER TABLE eka.document_files ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY files_read ON eka.document_files FOR SELECT
        USING (document_id IN (SELECT eka.readable_document_ids()))
    """,
    """
    CREATE POLICY files_insert ON eka.document_files FOR INSERT
        WITH CHECK (eka.owns_document(document_id))
    """,
    "ALTER TABLE eka.chunks ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY chunks_read ON eka.chunks FOR SELECT
        USING (document_id IN (SELECT eka.readable_document_ids()))
    """,
    "ALTER TABLE eka.ingestion_jobs ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY jobs_insert ON eka.ingestion_jobs FOR INSERT
        WITH CHECK (eka.owns_document(document_id))
    """,
    # ------------------------------------------------------------------ privileges
    "GRANT USAGE ON SCHEMA eka TO eka_app, eka_worker",
    # API (subject to the policies above)
    "GRANT SELECT, INSERT, DELETE ON eka.documents TO eka_app",
    "GRANT SELECT, INSERT ON eka.document_files, eka.document_grants TO eka_app",
    "GRANT SELECT ON eka.chunks TO eka_app",
    "GRANT INSERT ON eka.ingestion_jobs TO eka_app",
    "GRANT SELECT, INSERT ON eka.audit_events TO eka_app",
    # Ingestion worker (BYPASSRLS; never serves user requests)
    "GRANT SELECT, UPDATE ON eka.documents TO eka_worker",
    "GRANT SELECT ON eka.document_files TO eka_worker",
    "GRANT SELECT, INSERT, DELETE ON eka.chunks TO eka_worker",
    "GRANT SELECT, UPDATE ON eka.ingestion_jobs TO eka_worker",
    "GRANT INSERT ON eka.audit_events TO eka_worker",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only; restore from a backup instead.")
