# Metis frontend

Run from the repository root after starting the API and worker:

```bash
uv run --locked --project src/frontend streamlit run src/frontend/app.py
```

Set `BACKEND_URL` to the API URL (default `http://localhost:8000`). Sign in with an existing account or create one, then create an organisation when prompted. Owners and admins can manage sources, upload PDF/DOCX/TXT/Markdown files, retry failed ingestion, and add members. Members can view documents, search by chat, and read citations. The workspace has Dashboard, Knowledge, Chat, and Settings pages; each is scoped to the selected organisation.

The frontend keeps the bearer token in the Streamlit session and in an encrypted browser cookie, so refreshing the page does not sign you out. Set `METIS_SESSION_COOKIE_SECRET` to a unique random value for stable cookie encryption across frontend restarts; keep it separate from the API's `METIS_AUTH_SECRET_KEY`. The API bearer token still expires after 30 minutes. The backend must have `METIS_AUTH_SECRET_KEY` configured and a worker queue available for uploads and retries. The local Compose stack supplies development defaults.

Tests use Streamlit AppTest and mocked HTTP responses to cover the login state, workspace pages, API errors, token requests, and multipart upload formatting.
