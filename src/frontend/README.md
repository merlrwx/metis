# Metis frontend

Run from the repository root:

```bash
uv run --locked --project src/frontend streamlit run src/frontend/app.py
```

Set `BACKEND_URL` to the API URL (default `http://localhost:8000`). The landing page shows the current project stage and backend connection status. Upload and chat screens are planned for Phase 7.

Tests use Streamlit AppTest and mock HTTP responses to cover connected, unavailable and invalid backend states.
