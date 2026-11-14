"""Home, organisation overview, knowledge library, documents, sources and groups."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from metis_web.backend import ApiError, Backend
from metis_web.rendering import redirect, render
from metis_web.session import require_csrf
from metis_web.views.common import act, api_org, current_user, form_list, workspace

router = APIRouter(dependencies=[Depends(require_csrf)])

MAX_BATCH_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
ACCEPTED_TYPES = ".pdf,.docx,.txt,.md,.markdown,.csv"
LAST_ORG_COOKIE = "metis_org"
BUSY = {"pending", "queued", "processing"}


def require_admin(context: dict) -> None:
    if not context["is_admin"]:
        raise HTTPException(
            status_code=403, detail="An organisation owner or admin can do this."
        )


@router.get("/")
async def home(request: Request):
    if not request.state.auth:
        return RedirectResponse("/login", status_code=303)
    user = await current_user(request)
    memberships = user.get("memberships", [])
    if not memberships or request.query_params.get("new"):
        return render(request, "onboarding.html", {"user": user, "orgs": memberships})
    remembered = request.cookies.get(LAST_ORG_COOKIE)
    chosen = next(
        (item for item in memberships if item["organisation_id"] == remembered),
        memberships[0],
    )
    return RedirectResponse(f"/o/{chosen['organisation_id']}", status_code=303)


@router.post("/organisations")
async def create_organisation(
    request: Request, name: Annotated[str, Form(max_length=255)]
):
    try:
        organisation = await Backend(request).post(
            "/api/organisations", json={"name": name}
        )
    except ApiError as error:
        if error.status_code != 422:
            raise
        user = await current_user(request)
        return render(
            request,
            "onboarding.html",
            {"user": user, "error": "Enter a name for your organisation."},
            status_code=400,
        )
    return redirect(
        request,
        f"/o/{organisation['id']}",
        f"{organisation['name']} is ready. Upload your first documents to begin.",
    )


def _remember(response: Response, request: Request, org_id: UUID) -> Response:
    response.set_cookie(
        LAST_ORG_COOKIE,
        str(org_id),
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@router.get("/o/{org_id}")
async def overview(request: Request, org_id: UUID):
    context = await workspace(request, org_id)
    documents = await Backend(request).get(f"{api_org(org_id)}/documents")
    active = [item for item in documents if item.get("ingestion_status") != "deleted"]
    stats = {
        "documents": len(active),
        "indexed": sum(
            item.get("ingestion_status") in {"indexed", "completed"} for item in active
        ),
        "processing": sum(item.get("ingestion_status") in BUSY for item in active),
        "failed": [item for item in active if item.get("ingestion_status") == "failed"],
    }
    recent = sorted(active, key=lambda item: item.get("updated_at", ""), reverse=True)
    response = render(
        request,
        "overview.html",
        {**context, "nav": "overview", "stats": stats, "recent": recent[:6]},
    )
    return _remember(response, request, org_id)


async def _knowledge_page(
    request: Request, org_id: UUID, extra: dict | None = None, status_code=200
):
    context = await workspace(request, org_id)
    backend = Backend(request)
    documents = await backend.get(f"{api_org(org_id)}/documents")
    sources = await backend.get(f"{api_org(org_id)}/sources")
    groups = await backend.get(f"{api_org(org_id)}/groups")
    query = (request.query_params.get("q") or "").strip()
    visible = [item for item in documents if item.get("ingestion_status") != "deleted"]
    if query:
        visible = [
            item for item in visible if query.casefold() in item["title"].casefold()
        ]
    source_names = {item["id"]: item["name"] for item in sources}
    return render(
        request,
        "knowledge.html",
        {
            **context,
            "nav": "knowledge",
            "documents": visible,
            "sources": sources,
            "upload_sources": [item for item in sources if item["type"] == "upload"],
            "source_names": source_names,
            "groups": groups,
            "query": query,
            "busy": any(item.get("ingestion_status") in BUSY for item in visible),
            "accepted_types": ACCEPTED_TYPES,
            "max_files": MAX_BATCH_FILES,
            **(extra or {}),
        },
        status_code=status_code,
    )


@router.get("/o/{org_id}/knowledge")
async def knowledge(request: Request, org_id: UUID):
    return await _knowledge_page(request, org_id)


async def _upload(
    backend: Backend,
    org_id: UUID,
    upload: UploadFile,
    data: dict,
) -> dict:
    content = await upload.read(MAX_FILE_BYTES + 1)
    name = upload.filename or "document"
    if len(content) > MAX_FILE_BYTES:
        return {"ok": False, "name": name, "message": "is larger than 20 MiB."}
    try:
        result = await backend.post(
            f"{api_org(org_id)}/documents/upload",
            data={key: value for key, value in data.items() if value},
            files={
                "file": (
                    name,
                    content,
                    upload.content_type or "application/octet-stream",
                )
            },
        )
    except ApiError as error:
        return {"ok": False, "name": name, "message": error.detail}
    return {
        "ok": True,
        "name": result["document"]["title"],
        "message": "uploaded and queued for indexing.",
    }


@router.post("/o/{org_id}/documents")
async def upload_documents(request: Request, org_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    form = await request.form()
    files = [
        item
        for item in form.getlist("files")
        if hasattr(item, "read") and item.filename
    ]
    conflict = form.get("conflict_action") or "reject"
    if conflict not in {"reject", "replace", "new"}:
        conflict = "reject"
    if not files:
        return await _knowledge_page(
            request, org_id, {"upload_error": "Choose at least one file."}, 400
        )
    if len(files) > MAX_BATCH_FILES:
        return await _knowledge_page(
            request,
            org_id,
            {"upload_error": f"Choose at most {MAX_BATCH_FILES} files at a time."},
            400,
        )
    backend = Backend(request)
    data = {"source_id": form.get("source_id") or None, "conflict_action": conflict}
    results = [await _upload(backend, org_id, item, data) for item in files]
    return await _knowledge_page(request, org_id, {"upload_results": results})


@router.get("/o/{org_id}/documents/{document_id}")
async def document_detail(request: Request, org_id: UUID, document_id: UUID):
    context = await workspace(request, org_id)
    detail = await Backend(request).get(
        f"{api_org(org_id)}/documents/{document_id}/detail"
    )
    return render(
        request,
        "document.html",
        {
            **context,
            "nav": "knowledge",
            "detail": detail,
            "document": detail["document"],
            "accepted_types": ACCEPTED_TYPES,
        },
    )


async def _document_action(
    request: Request,
    org_id: UUID,
    document_id: UUID,
    method: str,
    suffix: str,
    target: str,
    success: str,
    **kwargs,
):
    context = await workspace(request, org_id)
    require_admin(context)
    return await act(
        request,
        target,
        success,
        Backend(request).call(
            method, f"{api_org(org_id)}/documents/{document_id}{suffix}", **kwargs
        ),
    )


@router.post("/o/{org_id}/documents/{document_id}/rename")
async def rename_document(
    request: Request,
    org_id: UUID,
    document_id: UUID,
    title: Annotated[str, Form(max_length=512)],
):
    return await _document_action(
        request,
        org_id,
        document_id,
        "PATCH",
        "",
        f"/o/{org_id}/documents/{document_id}",
        "Document renamed.",
        json={"title": title},
    )


@router.post("/o/{org_id}/documents/{document_id}/reindex")
async def reindex_document(request: Request, org_id: UUID, document_id: UUID):
    return await _document_action(
        request,
        org_id,
        document_id,
        "POST",
        "/reindex",
        f"/o/{org_id}/documents/{document_id}",
        "Document queued for re-indexing.",
    )


@router.post("/o/{org_id}/documents/{document_id}/retry")
async def retry_document(
    request: Request,
    org_id: UUID,
    document_id: UUID,
    back: Annotated[Literal["overview", "knowledge", "document"], Form()] = "document",
):
    target = {
        "overview": f"/o/{org_id}",
        "knowledge": f"/o/{org_id}/knowledge",
        "document": f"/o/{org_id}/documents/{document_id}",
    }[back]
    return await _document_action(
        request,
        org_id,
        document_id,
        "POST",
        "/retry",
        target,
        "Ingestion queued again.",
    )


@router.post("/o/{org_id}/documents/{document_id}/remove")
async def remove_document(request: Request, org_id: UUID, document_id: UUID):
    return await _document_action(
        request,
        org_id,
        document_id,
        "DELETE",
        "",
        f"/o/{org_id}/knowledge",
        "Document removed from the library. Stored history is retained.",
    )


@router.post("/o/{org_id}/documents/{document_id}/replace")
async def replace_document(
    request: Request, org_id: UUID, document_id: UUID, file: UploadFile
):
    context = await workspace(request, org_id)
    require_admin(context)
    result = await _upload(
        Backend(request),
        org_id,
        file,
        {"conflict_action": "replace", "replace_document_id": str(document_id)},
    )
    if not result["ok"]:
        return redirect(
            request,
            f"/o/{org_id}/documents/{document_id}",
            f"{result['name']}: {result['message']}",
            "error",
        )
    return redirect(
        request,
        f"/o/{org_id}/documents/{document_id}",
        "Replacement queued. The previous version is excluded from new answers.",
    )


@router.get("/o/{org_id}/documents/{document_id}/versions/{version_id}/download")
async def download_version(
    request: Request, org_id: UUID, document_id: UUID, version_id: UUID
):
    await workspace(request, org_id)
    download = await Backend(request).download(
        f"{api_org(org_id)}/documents/{document_id}/versions/{version_id}/download"
    )
    disposition = download.content_disposition
    if not disposition.lower().startswith("attachment"):
        disposition = "attachment"
    return Response(
        content=download.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@router.post("/o/{org_id}/sources")
async def create_source(
    request: Request,
    org_id: UUID,
    name: Annotated[str, Form(max_length=255)],
    kind: Annotated[Literal["upload", "microsoft365"], Form()] = "upload",
    approved: Annotated[bool, Form()] = False,
    auto_sync: Annotated[bool, Form()] = False,
):
    context = await workspace(request, org_id)
    require_admin(context)
    if kind == "microsoft365" and not approved:
        return redirect(
            request,
            f"/o/{org_id}/knowledge#sources",
            "Confirm organisation-wide library approval before adding this source.",
            "error",
        )
    return await act(
        request,
        f"/o/{org_id}/knowledge#sources",
        "Source added.",
        Backend(request).post(
            f"{api_org(org_id)}/sources",
            json={
                "name": name,
                "type": kind,
                "organisation_library_approved": kind == "microsoft365" and approved,
                "configuration": {"sync_enabled": auto_sync}
                if kind == "microsoft365"
                else {},
            },
        ),
    )


@router.post("/o/{org_id}/sources/{source_id}/sync")
async def sync_source(request: Request, org_id: UUID, source_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    return await act(
        request,
        f"/o/{org_id}/knowledge#sources",
        lambda result: (
            "Library synchronisation is in progress."
            if result and result.get("sync_status") == "syncing"
            else "Library synchronisation queued. Refresh to check progress."
        ),
        Backend(request).post(f"{api_org(org_id)}/sources/{source_id}/sync"),
    )


@router.post("/o/{org_id}/sources/{source_id}/check")
async def check_source(request: Request, org_id: UUID, source_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    try:
        result = await Backend(request).post(
            f"{api_org(org_id)}/sources/{source_id}/check"
        )
    except ApiError as error:
        if error.status_code >= 500:
            raise
        result = {"connected": False, "error": error.detail}
    if result and result.get("connected"):
        return redirect(
            request,
            f"/o/{org_id}/knowledge#sources",
            f"Connection confirmed · {result.get('library_name') or 'library'}",
        )
    return redirect(
        request,
        f"/o/{org_id}/knowledge#sources",
        (result or {}).get("error") or "The connection check failed.",
        "error",
    )


@router.post("/o/{org_id}/sources/{source_id}/settings")
async def source_settings(
    request: Request,
    org_id: UUID,
    source_id: UUID,
    sync_enabled: Annotated[bool, Form()] = False,
    paused: Annotated[bool, Form()] = False,
):
    context = await workspace(request, org_id)
    require_admin(context)
    return await act(
        request,
        f"/o/{org_id}/knowledge#sources",
        "Library settings saved.",
        Backend(request).call(
            "PATCH",
            f"{api_org(org_id)}/sources/{source_id}",
            json={"sync_enabled": sync_enabled, "paused": paused},
        ),
    )


@router.post("/o/{org_id}/sources/{source_id}/disconnect")
async def disconnect_source(request: Request, org_id: UUID, source_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    return await act(
        request,
        f"/o/{org_id}/knowledge#sources",
        "Source disconnected. Saved groups may now be empty; stored history remains.",
        Backend(request).call("DELETE", f"{api_org(org_id)}/sources/{source_id}"),
    )


@router.post("/o/{org_id}/groups")
async def create_group(request: Request, org_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    form = await request.form()
    return await act(
        request,
        f"/o/{org_id}/knowledge#groups",
        "Group created.",
        Backend(request).post(
            f"{api_org(org_id)}/groups",
            json={
                "name": form.get("name") or "",
                "source_ids": form_list(form, "source_ids"),
            },
        ),
    )


@router.post("/o/{org_id}/groups/{group_id}")
async def update_group(request: Request, org_id: UUID, group_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    form = await request.form()
    return await act(
        request,
        f"/o/{org_id}/knowledge#groups",
        "Group saved.",
        Backend(request).call(
            "PUT",
            f"{api_org(org_id)}/groups/{group_id}",
            json={
                "name": form.get("name") or "",
                "source_ids": form_list(form, "source_ids"),
            },
        ),
    )


@router.post("/o/{org_id}/groups/{group_id}/delete")
async def delete_group(request: Request, org_id: UUID, group_id: UUID):
    context = await workspace(request, org_id)
    require_admin(context)
    return await act(
        request,
        f"/o/{org_id}/knowledge#groups",
        "Group deleted.",
        Backend(request).call("DELETE", f"{api_org(org_id)}/groups/{group_id}"),
    )
