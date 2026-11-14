"""Calls to the Metis API on behalf of the signed-in browser session."""

import asyncio
import hashlib
import time
from dataclasses import dataclass

import httpx
from fastapi import Request

REFRESH_REUSE_SECONDS = 60


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class SignedOut(Exception):
    """The session cannot be used or refreshed; the browser must sign in again."""


def error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list):
        messages = [
            str(item.get("msg", "")).removeprefix("Value error, ")
            for item in detail
            if isinstance(item, dict)
        ]
        if any(messages):
            return "; ".join(message for message in messages if message)
    if response.status_code == 429:
        return "Too many requests. Wait a moment and try again."
    if response.status_code >= 500:
        return "Metis is temporarily unavailable. Try again shortly."
    return f"Request failed ({response.status_code})."


class RefreshCoordinator:
    """Serialise refreshes so concurrent requests never present a rotated token.

    The API revokes a session when a rotated-out refresh token is reused, so two
    requests from one browser that both see an expired access token must share
    a single refresh result.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._recent: dict[str, tuple[float, dict]] = {}

    async def refresh(self, client: httpx.AsyncClient, refresh_token: str) -> dict:
        key = hashlib.sha256(refresh_token.encode()).hexdigest()
        async with self._lock:
            now = time.monotonic()
            self._recent = {
                item: value
                for item, value in self._recent.items()
                if now - value[0] < REFRESH_REUSE_SECONDS
            }
            if key in self._recent:
                return self._recent[key][1]
            try:
                response = await client.post(
                    "/api/auth/refresh", json={"refresh_token": refresh_token}
                )
            except httpx.HTTPError as error:
                raise ApiError(503, "Metis is temporarily unavailable.") from error
            if response.status_code in (400, 401, 403, 422):
                raise SignedOut()
            if response.status_code != 200:
                raise ApiError(response.status_code, error_detail(response))
            tokens = response.json()
            self._recent[key] = (now, tokens)
            return tokens


@dataclass
class Download:
    content: bytes
    content_disposition: str


class Backend:
    def __init__(self, request: Request):
        self.request = request
        self.client: httpx.AsyncClient = request.app.state.http
        self.coordinator: RefreshCoordinator = request.app.state.refresh

    @property
    def tokens(self) -> dict | None:
        return self.request.state.auth

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise ApiError(504, "Metis took too long to respond. Try again.") from error
        except httpx.HTTPError as error:
            raise ApiError(503, "Metis is temporarily unavailable.") from error

    async def _authorised(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.tokens:
            raise SignedOut()
        headers = {"Authorization": f"Bearer {self.tokens['a']}"}
        response = await self._send(method, path, headers=headers, **kwargs)
        if response.status_code != 401:
            return response
        refreshed = await self.coordinator.refresh(self.client, self.tokens["r"])
        self.request.state.auth = {
            "a": refreshed["access_token"],
            "r": refreshed["refresh_token"],
        }
        self.request.state.auth_changed = True
        headers = {"Authorization": f"Bearer {refreshed['access_token']}"}
        response = await self._send(method, path, headers=headers, **kwargs)
        if response.status_code == 401:
            raise SignedOut()
        return response

    async def call(self, method: str, path: str, **kwargs):
        response = await self._authorised(method, path, **kwargs)
        if response.status_code >= 400:
            raise ApiError(response.status_code, error_detail(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get(self, path: str, **kwargs):
        return await self.call("GET", path, **kwargs)

    async def post(self, path: str, **kwargs):
        return await self.call("POST", path, **kwargs)

    async def download(self, path: str) -> Download:
        response = await self._authorised("GET", path)
        if response.status_code >= 400:
            raise ApiError(response.status_code, error_detail(response))
        return Download(
            content=response.content,
            content_disposition=response.headers.get(
                "content-disposition", "attachment"
            ),
        )

    async def public(self, method: str, path: str, **kwargs):
        response = await self._send(method, path, **kwargs)
        if response.status_code >= 400:
            raise ApiError(response.status_code, error_detail(response))
        return response.json() if response.content else None

    async def sign_out(self) -> None:
        if not self.tokens:
            return
        try:
            # Refreshes first if needed so the server-side session is revoked.
            await self.call("POST", "/api/auth/logout")
        except (ApiError, SignedOut):
            pass  # The cookie is cleared regardless; the session expires server-side.
