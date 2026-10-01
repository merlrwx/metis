# Metis API

Run from the repository root after starting PostgreSQL:

```bash
docker compose up -d database
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend alembic -c src/backend/alembic.ini upgrade head
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend metis-api
```

The container runs the migration before starting the API. `HOST` defaults to `0.0.0.0`; `PORT` defaults to `8000`; `DATABASE_URL` defaults to the local Compose database.

- `GET /health`: database connectivity.
- `GET /api/info`: application identity and current stage.
- `POST /api/organisations`: create an organisation.
- `POST /api/organisations/{id}/sources`: create a tenant-owned source.
- `POST /api/organisations/{id}/documents`: create document metadata, with an optional tenant-owned source.
- `GET /api/organisations/{id}/documents`: list metadata scoped to that organisation.
- `/docs`: interactive OpenAPI documentation.

The development API does not yet require authentication; tenant authentication and membership enforcement are planned in Phase 6. The Compose password is for local use only.
