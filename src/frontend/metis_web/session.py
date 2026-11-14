"""Encrypted HttpOnly session cookie and signed double-submit CSRF tokens.

Tokens never reach browser JavaScript: the cookie is HttpOnly and its value is
Fernet-encrypted (authenticated), so it is opaque even to the browser.
"""

import hashlib
import hmac
import json
import secrets
from base64 import urlsafe_b64encode
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

SESSION_COOKIE = "metis_session"
FLASH_COOKIE = "metis_flash"
CSRF_COOKIE = "metis_csrf"


class SessionCodec:
    def __init__(self, secret: str):
        derived = hashlib.sha256(("metis-session:" + secret).encode()).digest()
        self._fernet = Fernet(urlsafe_b64encode(derived))
        self._csrf_key = hashlib.sha256(("metis-csrf:" + secret).encode()).digest()

    def encode(self, data: dict) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def decode(self, value: str | None, max_age: int) -> dict | None:
        if not value:
            return None
        try:
            data = json.loads(self._fernet.decrypt(value.encode(), ttl=max_age))
        except (InvalidToken, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("a") or not data.get("r"):
            return None
        return data

    def encode_flash(self, kind: str, message: str) -> str:
        return self._fernet.encrypt(
            json.dumps({"flash": kind, "message": message}).encode()
        ).decode()

    def decode_flash(self, value: str | None) -> dict | None:
        if not value:
            return None
        try:
            data = json.loads(self._fernet.decrypt(value.encode(), ttl=120))
        except (InvalidToken, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or "flash" not in data:
            return None
        return {"kind": str(data["flash"]), "message": str(data.get("message", ""))}

    def csrf_token(self, csrf_cookie: str) -> str:
        return hmac.new(self._csrf_key, csrf_cookie.encode(), "sha256").hexdigest()

    def csrf_valid(self, csrf_cookie: str | None, submitted: str | None) -> bool:
        if not csrf_cookie or not submitted:
            return False
        return hmac.compare_digest(self.csrf_token(csrf_cookie), submitted)


def new_csrf_cookie() -> str:
    return secrets.token_urlsafe(32)


async def require_csrf(request: Request) -> None:
    """Reject state-changing requests without a valid signed CSRF token."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    submitted = request.headers.get("X-CSRF-Token")
    if submitted is None:
        form = await request.form()
        value = form.get("csrf_token")
        submitted = value if isinstance(value, str) else None
    codec: SessionCodec = request.app.state.codec
    if not codec.csrf_valid(request.cookies.get(CSRF_COOKIE), submitted):
        raise HTTPException(
            status_code=403,
            detail="This form expired. Reload the page and try again.",
        )


def safe_next(value: str | None, default: str = "/") -> str:
    """Only allow same-site relative redirects."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or "\\" in value:
        return default
    return value
