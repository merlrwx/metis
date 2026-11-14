"""Sign in, registration, sign out and invitation links."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from metis_web.backend import ApiError, Backend
from metis_web.rendering import redirect, render
from metis_web.session import require_csrf, safe_next
from metis_web.views.common import current_user

router = APIRouter(dependencies=[Depends(require_csrf)])


async def _start_session(request: Request, email: str, password: str) -> None:
    tokens = await Backend(request).public(
        "POST",
        "/api/auth/token",
        data={"username": email, "password": password},
        headers={"User-Agent": request.headers.get("user-agent", "metis-web")[:255]},
    )
    request.state.auth = {"a": tokens["access_token"], "r": tokens["refresh_token"]}
    request.state.auth_changed = True


@router.get("/login")
async def login_page(request: Request, next: str = "/"):
    if request.state.auth:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next), "email": ""})


@router.post("/login")
async def login(
    request: Request,
    email: Annotated[str, Form(max_length=320)],
    password: Annotated[str, Form(max_length=256)],
    next: Annotated[str, Form()] = "/",
):
    try:
        await _start_session(request, email, password)
    except ApiError as error:
        message = (
            "That email and password do not match."
            if error.status_code == 401
            else error.detail
        )
        return render(
            request,
            "login.html",
            {"next": safe_next(next), "email": email, "error": message},
            status_code=400 if error.status_code < 500 else error.status_code,
        )
    return RedirectResponse(safe_next(next), status_code=303)


@router.get("/register")
async def register_page(request: Request, next: str = "/", email: str = ""):
    if request.state.auth:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(
        request,
        "register.html",
        {"next": safe_next(next), "email": email[:320], "name": ""},
    )


@router.post("/register")
async def register(
    request: Request,
    name: Annotated[str, Form(max_length=255)],
    email: Annotated[str, Form(max_length=320)],
    password: Annotated[str, Form(max_length=256)],
    next: Annotated[str, Form()] = "",
):
    try:
        await Backend(request).public(
            "POST",
            "/api/auth/register",
            json={"email": email, "name": name, "password": password},
        )
        await _start_session(request, email, password)
    except ApiError as error:
        return render(
            request,
            "register.html",
            {
                "next": safe_next(next),
                "email": email,
                "name": name,
                "error": error.detail,
            },
            status_code=400 if error.status_code < 500 else error.status_code,
        )
    if next and safe_next(next) != "/":
        return RedirectResponse(safe_next(next), status_code=303)
    user = await current_user(request)
    return render(request, "onboarding.html", {"user": user, "welcome": True})


@router.post("/logout")
async def logout(request: Request):
    await Backend(request).sign_out()
    request.state.auth = None
    request.state.auth_changed = True
    return redirect(request, "/login", "You have signed out.")


@router.get("/invite/{token}")
async def invitation(request: Request, token: str):
    try:
        preview = await Backend(request).public(
            "POST", "/api/invitations/preview", json={"token": token}
        )
    except ApiError as error:
        if error.status_code not in (404, 422):
            raise
        return render(
            request,
            "error.html",
            {
                "title": "Invitation unavailable",
                "message": "This invitation link has expired, was revoked or was "
                "already used. Ask an organisation admin for a new link.",
            },
            status_code=404,
        )
    user = await current_user(request) if request.state.auth else None
    return render(
        request,
        "invite.html",
        {
            "preview": preview,
            "token": token,
            "invite_path": f"/invite/{token}",
            "user": user,
            "email_matches": bool(user) and user["email"] == preview["email"],
        },
    )


@router.post("/invite/{token}/accept")
async def accept_invitation(request: Request, token: str):
    accepted = await Backend(request).post(
        "/api/invitations/accept", json={"token": token}
    )
    return redirect(
        request,
        f"/o/{accepted['organisation_id']}",
        "Welcome! You have joined the organisation.",
    )
