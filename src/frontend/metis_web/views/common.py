"""Helpers shared by the page routers."""

from collections.abc import Awaitable
from uuid import UUID

from fastapi import HTTPException, Request

from metis_web.backend import ApiError, Backend
from metis_web.rendering import redirect

ADMIN_ROLES = {"owner", "admin"}


async def current_user(request: Request) -> dict:
    user = await Backend(request).get("/api/auth/me")
    request.state.page = {**getattr(request.state, "page", {}), "user": user}
    return user


async def workspace(request: Request, org_id: UUID) -> dict:
    """Load the signed-in user and their membership of this organisation."""
    user = await current_user(request)
    memberships = user.get("memberships", [])
    membership = next(
        (item for item in memberships if item["organisation_id"] == str(org_id)),
        None,
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    context = {
        "user": user,
        "orgs": memberships,
        "org": {"id": str(org_id), "name": membership["organisation_name"]},
        "role": membership["role"],
        "is_admin": membership["role"] in ADMIN_ROLES,
        "is_owner": membership["role"] == "owner",
    }
    request.state.page = {**getattr(request.state, "page", {}), **context}
    request.state.org_id = str(org_id)
    return context


def api_org(org_id: UUID | str) -> str:
    return f"/api/organisations/{org_id}"


def form_list(form, name: str) -> list[str]:
    return [value for value in form.getlist(name) if isinstance(value, str) and value]


async def act(request: Request, target: str, success: str, action: Awaitable):
    """Run an API action and come back to the page with a success or error notice."""
    try:
        result = await action
    except ApiError as error:
        if error.status_code >= 500:
            raise
        return redirect(request, target, error.detail, "error")
    if callable(success):
        success = success(result)
    return redirect(request, target, success)
