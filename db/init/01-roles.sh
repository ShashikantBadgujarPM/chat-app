#!/bin/sh
# Cluster-level roles only (docs/design/12 §22.5, Q-004). Runs once, on an empty data
# volume. chat_owner is the image's POSTGRES_USER. Grants, default privileges and the
# role's timeout are applied by Alembic migration 0001 in every database.
set -eu

if [ -z "${CHAT_APP_PASSWORD:-}" ]; then
    echo "CHAT_APP_PASSWORD must be set" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v app_password="$CHAT_APP_PASSWORD" <<'SQL'
CREATE ROLE chat_app LOGIN PASSWORD :'app_password';
SQL
