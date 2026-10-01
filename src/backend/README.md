# Metis API

Run from the repository root:

```bash
uv run --locked --project src/backend metis-api
```

`HOST` defaults to `0.0.0.0`, and `PORT` defaults to `8000`.

- `GET /health`: process liveness (`{"status":"ok"}`).
- `GET /api/info`: application identity and implementation stage.
- `/docs`: interactive OpenAPI documentation.

This stateless foundation has no database or authentication yet. PostgreSQL and tenant-owned metadata are the next planned phase.
