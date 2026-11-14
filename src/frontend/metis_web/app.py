"""Metis web interface: a small server-rendered BFF in front of the Metis API."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from metis_web import views
from metis_web.backend import ApiError, RefreshCoordinator, SignedOut
from metis_web.config import Settings
from metis_web.limits import RequestSizeLimit
from metis_web.rendering import render, templates
from metis_web.session import (
    CSRF_COOKIE,
    FLASH_COOKIE,
    SESSION_COOKIE,
    SessionCodec,
    new_csrf_cookie,
)

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def cookie_is_secure(request: Request) -> bool:
    setting = request.app.state.settings.cookie_secure
    if setting == "auto":
        return request.url.scheme == "https"
    return setting == "true"


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.http.aclose()

    app = FastAPI(
        title="Metis",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.codec = SessionCodec(settings.cookie_secret)
    app.state.refresh = RefreshCoordinator()
    app.state.http = httpx.AsyncClient(
        base_url=settings.backend_url,
        transport=transport,
        timeout=httpx.Timeout(30.0, read=settings.chat_timeout),
        headers={"User-Agent": "metis-web"},
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(RequestSizeLimit)
    app.include_router(views.router)

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        codec: SessionCodec = app.state.codec
        request.state.auth = codec.decode(
            request.cookies.get(SESSION_COOKIE), settings.session_max_age
        )
        request.state.auth_changed = False
        csrf_cookie = request.cookies.get(CSRF_COOKIE)
        request.state.csrf_cookie = csrf_cookie or new_csrf_cookie()
        request.state.flash_out = None
        request.state.flash_consumed = False
        response = await call_next(request)
        secure = cookie_is_secure(request)
        if request.state.auth_changed:
            if request.state.auth:
                response.set_cookie(
                    SESSION_COOKIE,
                    codec.encode(request.state.auth),
                    max_age=settings.session_max_age,
                    httponly=True,
                    secure=secure,
                    samesite="lax",
                    path="/",
                )
            else:
                response.delete_cookie(
                    SESSION_COOKIE,
                    path="/",
                    httponly=True,
                    secure=secure,
                    samesite="lax",
                )
        elif request.cookies.get(SESSION_COOKIE) and request.state.auth is None:
            response.delete_cookie(
                SESSION_COOKIE, path="/", httponly=True, secure=secure, samesite="lax"
            )
        if not csrf_cookie:
            response.set_cookie(
                CSRF_COOKIE,
                request.state.csrf_cookie,
                httponly=True,
                secure=secure,
                samesite="lax",
                path="/",
            )
        if request.state.flash_out is not None:
            response.set_cookie(
                FLASH_COOKIE,
                codec.encode_flash(*request.state.flash_out),
                max_age=120,
                httponly=True,
                secure=secure,
                samesite="lax",
                path="/",
            )
        elif request.state.flash_consumed:
            response.delete_cookie(
                FLASH_COOKIE, path="/", httponly=True, secure=secure, samesite="lax"
            )
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(SignedOut)
    async def signed_out(request: Request, _: SignedOut):
        request.state.auth = None
        request.state.auth_changed = True
        target = "/login?next=" + quote(
            request.url.path if request.method == "GET" else "/", safe="/"
        )
        if request.headers.get("HX-Request"):
            response = HTMLResponse("", status_code=200)
            response.headers["HX-Redirect"] = target
            return response
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        status = error.status_code if 400 <= error.status_code < 600 else 502
        return render(
            request,
            "error.html",
            {"title": "Something went wrong", "message": error.detail},
            status_code=status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _: RequestValidationError):
        return render(
            request,
            "error.html",
            {
                "title": "Check the form",
                "message": "Some required information was missing or invalid.",
            },
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: HTTPException):
        titles = {403: "Not allowed", 404: "Not found", 405: "Not allowed"}
        return render(
            request,
            "error.html",
            {
                "title": titles.get(error.status_code, "Something went wrong"),
                "message": error.detail
                if isinstance(error.detail, str)
                else "The request could not be completed.",
            },
            status_code=error.status_code,
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    app.state.templates = templates
    return app


def main() -> None:
    uvicorn.run(
        "metis_web.app:create_app",
        factory=True,
        # Container default; set HOST to restrict the listening interface.
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("PORT", "8501")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
