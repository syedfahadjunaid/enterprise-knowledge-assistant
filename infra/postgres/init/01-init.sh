#!/bin/sh
# Runs once, the first time the Postgres volume is created (and in the integration tests,
# which mount this same file into their Testcontainers database).
#
# Least privilege: three logins for the application, each allowed only what it needs.
#
#   eka_owner   owns schema "eka" and runs the Alembic migrations (DDL). Nothing else uses it.
#   eka_app     the API. Plain DML on the tables it needs, and every read goes through
#               row-level security (RLS): it can only see rows of documents the signed-in user
#               may read. It is NOT the table owner, so RLS cannot be skipped by accident.
#   eka_worker  the ingestion worker. BYPASSRLS, because it processes every uploaded document
#               regardless of who may read it. It never serves user requests.
#   keycloak    owns the separate "keycloak" database.
#
# Table privileges for eka_app / eka_worker are granted by the migrations (they know the tables).
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v owner_pw="$EKA_OWNER_DB_PASSWORD" \
     -v app_pw="$EKA_APP_DB_PASSWORD" \
     -v worker_pw="$EKA_WORKER_DB_PASSWORD" \
     -v kc_pw="$KEYCLOAK_DB_PASSWORD" <<'EOSQL'
-- :'name' makes psql quote the value safely as a SQL literal
CREATE ROLE eka_owner  LOGIN PASSWORD :'owner_pw';
CREATE ROLE eka_app    LOGIN PASSWORD :'app_pw';
CREATE ROLE eka_worker LOGIN PASSWORD :'worker_pw' BYPASSRLS;
CREATE ROLE keycloak   LOGIN PASSWORD :'kc_pw';

CREATE DATABASE keycloak OWNER keycloak;
REVOKE CONNECT ON DATABASE keycloak FROM PUBLIC;

REVOKE CONNECT ON DATABASE eka FROM PUBLIC;
GRANT  CONNECT ON DATABASE eka TO eka_owner, eka_app, eka_worker;

-- Nobody may create objects in "public"; our tables live in schema "eka".
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA eka AUTHORIZATION eka_owner;

-- pgvector needs a superuser to install; the application roles only use it.
CREATE EXTENSION IF NOT EXISTS vector;

ALTER ROLE eka_owner  SET search_path = eka, public;
ALTER ROLE eka_app    SET search_path = eka, public;
ALTER ROLE eka_worker SET search_path = eka, public;
EOSQL
