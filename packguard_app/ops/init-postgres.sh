#!/bin/sh
set -eu

export PGPASSWORD="$POSTGRES_PASSWORD"
psql --no-psqlrc --set=ON_ERROR_STOP=1 \
  --host=database \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  --set=app_role="$PACKGUARD_APP_DB_USER" \
  --set=app_password="$PACKGUARD_APP_DB_PASSWORD" \
  --set=migration_role="$PACKGUARD_MIGRATOR_DB_USER" \
  --set=migration_password="$PACKGUARD_MIGRATOR_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I', :'migration_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migration_role')
\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'migration_role', :'migration_password')
\gexec
SELECT format('CREATE ROLE %I', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_role', :'app_password')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'migration_role')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role')
\gexec
GRANT USAGE, CREATE ON SCHEMA public TO :"migration_role";
GRANT USAGE ON SCHEMA public TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
SQL
