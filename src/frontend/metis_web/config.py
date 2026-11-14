"""Runtime settings, validated once at application start."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    backend_url: str
    cookie_secret: str
    cookie_secure: str  # "auto", "true" or "false"
    session_max_age: int
    chat_timeout: float
    public_url: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
        if urlsplit(backend_url).scheme not in {"http", "https"}:
            raise RuntimeError("BACKEND_URL must be an http(s) URL")
        secret = os.environ.get("METIS_SESSION_COOKIE_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError(
                "METIS_SESSION_COOKIE_SECRET must contain at least 32 characters"
            )
        secure = os.environ.get("METIS_COOKIE_SECURE", "auto").lower()
        if secure not in {"auto", "true", "false"}:
            raise RuntimeError("METIS_COOKIE_SECURE must be auto, true or false")
        days = int(os.environ.get("METIS_SESSION_DAYS", "30"))
        if not 1 <= days <= 365:
            raise RuntimeError("METIS_SESSION_DAYS must be between 1 and 365")
        public_url = os.environ.get("METIS_PUBLIC_URL", "").rstrip("/") or None
        if public_url and urlsplit(public_url).scheme not in {"http", "https"}:
            raise RuntimeError("METIS_PUBLIC_URL must be an http(s) URL")
        return cls(
            backend_url=backend_url,
            public_url=public_url,
            cookie_secret=secret,
            cookie_secure=secure,
            session_max_age=days * 24 * 60 * 60,
            chat_timeout=float(os.environ.get("METIS_CHAT_TIMEOUT", "300")),
        )
