#!/bin/sh
set -eu

attempt=0
while ! /app/.venv/bin/python -c 'from sqlalchemy import text; from backend.database import engine; connection=engine.connect(); connection.execute(text("SELECT 1")); connection.close()' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "PostgreSQL was unavailable during container startup" >&2
    exit 1
  fi
  sleep 1
done

/app/.venv/bin/alembic upgrade head
exec /app/.venv/bin/metis-api
