import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import streamlit as st

st.set_page_config(page_title="Metis", page_icon="📚")
st.title("Metis")
st.write("Grounded answers from your organisation's knowledge.")
st.info(
    "The application foundation is ready. Document upload and chat are coming next."
)

backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
try:
    with urlopen(f"{backend_url}/api/info", timeout=5) as response:
        info = json.load(response)
    if info.get("name") != "Metis" or info.get("stage") != "vector-search":
        raise ValueError("Unexpected backend response")
except (HTTPError, URLError, TimeoutError, OSError, ValueError):
    st.error("Cannot connect to the Metis API. Check that the backend is running.")
else:
    st.success("Metis API connected")

st.subheader("What's next")
st.write(
    "Organisations and document metadata, followed by ingestion and cited answers."
)
