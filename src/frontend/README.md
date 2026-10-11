# Metis frontend

Run from the repository root after starting the API and worker:

```bash
uv run --locked --project src/frontend streamlit run src/frontend/app.py
```

Set `BACKEND_URL` to the API URL (default `http://localhost:8000`). Sign in with an existing account or create one, then create an organisation when prompted. Owners and admins can manage sources, upload PDF/DOCX/TXT/Markdown files, retry failed ingestion, and add members. Members can view documents, search by chat, and read citations. The workspace has Dashboard, Knowledge, Chat, and Settings pages; each is scoped to the selected organisation.

Each answer shows its exact cited excerpt, source title, page or section, and available freshness information. Open web-backed citations in their source; owners and admins can request a bounded, read-only extracted-text preview through the authenticated document detail API. Members can still use the cited excerpt and authenticated original download when available. Retrieval runs before answer generation, which only proceeds when the evidence supports a response. Missing sources, indexing problems, insufficient evidence, and provider failures have separate guidance. Answer feedback is stored with the conversation for diagnostics and evaluation; it is not indexed as knowledge.

The frontend keeps the bearer token in the Streamlit session and in an encrypted browser cookie, so refreshing the page does not sign you out. Set `METIS_SESSION_COOKIE_SECRET` to a unique random value for stable cookie encryption across frontend restarts; keep it separate from the API's `METIS_AUTH_SECRET_KEY`. The API bearer token still expires after 30 minutes. The backend must have `METIS_AUTH_SECRET_KEY` configured and a worker queue available for uploads and retries. The local Compose stack supplies development defaults.

Tests use Streamlit AppTest and mocked HTTP responses to cover the login state, workspace pages, API errors, token requests, and multipart upload formatting.
