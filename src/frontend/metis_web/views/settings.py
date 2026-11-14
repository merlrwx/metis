"""Organisation settings, members and invitations; personal account and sessions."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request

from metis_web.backend import ApiError, Backend
from metis_web.rendering import redirect, render
from metis_web.session import require_csrf
from metis_web.views.common import act, api_org, current_user, workspace

router = APIRouter(dependencies=[Depends(require_csrf)])


def invitation_link(request: Request, token: str) -> str:
    public_url = request.app.state.settings.public_url
    base = public_url or str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}"


async def _settings_page(
    request: Request, org_id: UUID, extra: dict | None = None, status_code: int = 200
):
    context = await workspace(request, org_id)
    members, invitations = [], []
    if context["is_admin"]:
        backend = Backend(request)
        members = await backend.get(f"{api_org(org_id)}/members")
        invitations = await backend.get(f"{api_org(org_id)}/invitations")
    owners = sum(member["role"] == "owner" for member in members)
    return render(
        request,
        "settings.html",
        {
            **context,
            "nav": "settings",
            "members": members,
            "invitations": invitations,
            "owner_count": owners,
            **(extra or {}),
        },
        status_code=status_code,
    )


@router.get("/o/{org_id}/settings")
async def settings_page(request: Request, org_id: UUID):
    return await _settings_page(request, org_id)


@router.post("/o/{org_id}/settings/name")
async def rename_organisation(
    request: Request, org_id: UUID, name: Annotated[str, Form(max_length=255)]
):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/settings",
        "Organisation renamed.",
        Backend(request).call("PATCH", api_org(org_id), json={"name": name}),
    )


@router.post("/o/{org_id}/settings/members")
async def add_member(
    request: Request,
    org_id: UUID,
    email: Annotated[str, Form(max_length=320)],
    role: Annotated[Literal["member", "admin"], Form()] = "member",
):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/settings#members",
        "Member added.",
        Backend(request).post(
            f"{api_org(org_id)}/members", json={"email": email, "role": role}
        ),
    )


@router.post("/o/{org_id}/settings/members/{user_id}/role")
async def change_role(
    request: Request,
    org_id: UUID,
    user_id: UUID,
    role: Annotated[Literal["owner", "admin", "member"], Form()],
):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/settings#members",
        "Role updated.",
        Backend(request).call(
            "PATCH", f"{api_org(org_id)}/members/{user_id}", json={"role": role}
        ),
    )


@router.post("/o/{org_id}/settings/members/{user_id}/remove")
async def remove_member(request: Request, org_id: UUID, user_id: UUID):
    context = await workspace(request, org_id)
    leaving = str(user_id) == context["user"]["id"]
    try:
        await Backend(request).call("DELETE", f"{api_org(org_id)}/members/{user_id}")
    except ApiError as error:
        if error.status_code >= 500:
            raise
        return redirect(request, f"/o/{org_id}/settings#members", error.detail, "error")
    if leaving:
        return redirect(request, "/", f"You left {context['org']['name']}.")
    return redirect(request, f"/o/{org_id}/settings#members", "Member removed.")


@router.post("/o/{org_id}/settings/invitations")
async def create_invitation(
    request: Request,
    org_id: UUID,
    email: Annotated[str, Form(max_length=320)],
    role: Annotated[Literal["member", "admin"], Form()] = "member",
):
    await workspace(request, org_id)
    try:
        created = await Backend(request).post(
            f"{api_org(org_id)}/invitations", json={"email": email, "role": role}
        )
    except ApiError as error:
        if error.status_code >= 500:
            raise
        return redirect(
            request, f"/o/{org_id}/settings#invitations", error.detail, "error"
        )
    # The link is shown once; only a hash of its token is stored.
    return await _settings_page(
        request,
        org_id,
        {
            "new_invitation": {
                **created,
                "link": invitation_link(request, created["token"]),
            }
        },
    )


@router.post("/o/{org_id}/settings/invitations/{invitation_id}/revoke")
async def revoke_invitation(request: Request, org_id: UUID, invitation_id: UUID):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/settings#invitations",
        "Invitation revoked.",
        Backend(request).call(
            "DELETE", f"{api_org(org_id)}/invitations/{invitation_id}"
        ),
    )


async def _account_page(request: Request, extra: dict | None = None, status_code=200):
    user = await current_user(request)
    sessions = await Backend(request).get("/api/auth/sessions")
    return render(
        request,
        "account.html",
        {
            "user": user,
            "orgs": user.get("memberships", []),
            "nav": "account",
            "sessions": sessions,
            **(extra or {}),
        },
        status_code=status_code,
    )


@router.get("/account")
async def account(request: Request):
    return await _account_page(request)


@router.post("/account/profile")
async def update_profile(request: Request, name: Annotated[str, Form(max_length=255)]):
    return await act(
        request,
        "/account",
        "Profile saved.",
        Backend(request).call("PATCH", "/api/auth/me", json={"name": name}),
    )


@router.post("/account/password")
async def change_password(
    request: Request,
    current_password: Annotated[str, Form(max_length=256)],
    new_password: Annotated[str, Form(max_length=256)],
    confirm_password: Annotated[str, Form(max_length=256)],
):
    if new_password != confirm_password:
        return redirect(
            request, "/account#password", "The new passwords differ.", "error"
        )
    return await act(
        request,
        "/account#password",
        "Password changed. Your other sessions were signed out.",
        Backend(request).post(
            "/api/auth/password",
            json={"current_password": current_password, "new_password": new_password},
        ),
    )


@router.post("/account/sessions/{session_id}/revoke")
async def revoke_session(request: Request, session_id: UUID):
    return await act(
        request,
        "/account#sessions",
        "That session was signed out.",
        Backend(request).call("DELETE", f"/api/auth/sessions/{session_id}"),
    )


@router.post("/account/sessions/revoke-others")
async def revoke_other_sessions(request: Request):
    return await act(
        request,
        "/account#sessions",
        "All other sessions were signed out.",
        Backend(request).post("/api/auth/sessions/revoke-others"),
    )
