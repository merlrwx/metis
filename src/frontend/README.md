# Metis web

A server-rendered web interface (FastAPI, Jinja2 and vendored [htmx](https://htmx.org) 2.0.4) in front of the Metis API. Pages work without JavaScript; htmx adds in-place navigation, automatic status refresh and confirmation prompts.

Run from the repository root after starting the API and worker:

```bash
export METIS_SESSION_COOKIE_SECRET="$(openssl rand -hex 32)"
BACKEND_URL=http://localhost:8000 uv run --locked --project src/frontend metis-web
```

Open http://localhost:8501. Create an account, create an organisation (or open an invitation link), then upload documents and ask questions.

## Pages

| Page | What it does |
| --- | --- |
| Overview | Document counts, items that need attention, recent changes and a quick question box. |
| Ask | Cited chat with saved conversations, rename/delete, feedback, and optional scope (saved group, sources, documents). Retrying after an error reuses the request id, so a question is never answered twice. |
| Knowledge | Library with filtering and live status, batch uploads (up to 10 files, with skip/replace/keep-both for duplicate names), sources including Microsoft 365 controls, and saved groups. |
| Document | Extracted-text preview, versions with downloads, rename, replace, re-index, retry and remove. |
| Team & settings | Rename the organisation, invitation links, roles, member removal and leaving. |
| Account | Profile, password change and the list of signed-in sessions with remote sign-out. |

## Evidence and failures

Each answer shows its exact cited excerpts with source title, page or section, and freshness information when available. Web-backed citations open in their source. Owners and admins can open a bounded, read-only extracted-text preview with parsing warnings; members use the cited excerpt and the authenticated original download. Retrieval runs before answer generation, which only proceeds when the evidence supports a response. Partial answers list the related passages that were retrieved. Missing sources, indexing problems, insufficient evidence and provider failures each get separate guidance. Answer feedback is stored with the conversation for diagnostics and evaluation; it is not indexed as knowledge.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKEND_URL` | `http://localhost:8000` | Metis API base URL. |
| `METIS_SESSION_COOKIE_SECRET` | *(required)* | At least 32 characters. Encrypts the session cookie and signs CSRF tokens. Keep it separate from the API's `METIS_AUTH_SECRET_KEY`. The app refuses to start without it. |
| `METIS_COOKIE_SECURE` | `auto` | `true` behind HTTPS (recommended in production), `false` for plain-HTTP development, `auto` to follow the request scheme. |
| `METIS_PUBLIC_URL` | request URL | Public base URL used in invitation links, e.g. `https://metis.example.com`. |
| `METIS_SESSION_DAYS` | `30` | Browser cookie lifetime; match the API's `METIS_SESSION_DAYS`. |
| `METIS_CHAT_TIMEOUT` | `300` | Seconds to wait for an answer. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-Proto`/`For`. |

## Security model

- The browser holds one HttpOnly, SameSite=Lax cookie whose value is Fernet-encrypted. API access and refresh tokens never reach JavaScript.
- Expired access tokens are refreshed transparently. Refreshes are serialised per token, so concurrent requests never present a rotated-out refresh token; the API treats that as theft and revokes the session. If you run several frontend replicas, use sticky sessions to keep this guarantee.
- Every state-changing request needs a signed double-submit CSRF token: a hidden form field or the `X-CSRF-Token` header that htmx sends.
- A strict Content-Security-Policy applies: no inline scripts or styles, no eval, `frame-ancestors 'none'`. htmx's history cache is disabled, so page contents are never written to `localStorage`.
- Model output is rendered as Markdown with raw HTML disabled and unsafe link schemes removed. Downloads are always served as attachments.

## Tests

```bash
uv run --locked --project src/frontend pytest src/frontend/tests --cov=metis_web
```

Tests drive every page through FastAPI's test client against a scriptable fake API (`tests/conftest.py`). They cover redirects, CSRF, cookie handling, token refresh, error display, XSS escaping and the exact API calls each action makes.
