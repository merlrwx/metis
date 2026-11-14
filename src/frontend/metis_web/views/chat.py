"""Grounded, cited chat with saved conversations and scoped knowledge."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Request

from metis_web.backend import ApiError, Backend
from metis_web.rendering import render
from metis_web.session import require_csrf
from metis_web.views.common import act, api_org, form_list, workspace

router = APIRouter(dependencies=[Depends(require_csrf)])
PAGE_SIZE = 20
SUGGESTIONS = (
    "What should I do after a medication incident?",
    "Summarise the key points of our leave policy.",
    "Which procedure is the most recent version?",
)


def friendly_error(error: ApiError) -> str:
    """Separate guidance for unavailable sources, indexing and provider failures."""
    if error.status_code == 404:
        return (
            "A selected source or conversation is unavailable. Remove the "
            "unavailable filter and try again."
        )
    if error.status_code == 409 and "index" in error.detail.lower():
        return (
            "Knowledge indexing is in progress or needs attention. Try again later "
            "or ask an administrator to check indexing status."
        )
    if error.status_code == 503:
        return (
            "The embedding or answer provider is unavailable. Try again later; an "
            "administrator can check provider configuration."
        )
    return error.detail


def scope_from(values: dict) -> dict:
    """Normalise a submitted or saved scope into the API's KnowledgeScope shape."""
    scope: dict = {}
    if values.get("group_id"):
        scope["group_id"] = values["group_id"]
    elif values.get("source_ids"):
        scope["source_ids"] = list(values["source_ids"])[:20]
    documents = list(values.get("document_ids") or [])
    if values.get("document_id") and not documents:
        documents = [values["document_id"]]
    if documents:
        scope["document_ids"] = documents[:20]
    return scope


async def _chat_page(
    request: Request,
    org_id: UUID,
    conversation_id: UUID | None = None,
    extra: dict | None = None,
    status_code: int = 200,
):
    context = await workspace(request, org_id)
    backend = Backend(request)
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    conversations = await backend.get(
        f"{api_org(org_id)}/conversations",
        params={"limit": PAGE_SIZE, "offset": offset},
    )
    conversation = None
    if conversation_id:
        conversation = await backend.get(
            f"{api_org(org_id)}/conversations/{conversation_id}"
        )
    documents = await backend.get(f"{api_org(org_id)}/documents")
    sources = await backend.get(f"{api_org(org_id)}/sources")
    groups = await backend.get(f"{api_org(org_id)}/groups")
    ready = [item for item in documents if item.get("ingestion_status") == "indexed"]
    failed = sum(item.get("ingestion_status") == "failed" for item in documents)
    processing = sum(
        item.get("ingestion_status") in {"pending", "queued", "processing"}
        for item in documents
    )
    scope = (extra or {}).get("scope") or scope_from(
        (conversation or {}).get("scope") or {}
    )
    names = {
        **{item["id"]: item["title"] for item in documents},
        **{item["id"]: item["name"] for item in sources},
        **{item["id"]: item["name"] for item in groups},
    }
    return render(
        request,
        "chat.html",
        {
            **context,
            "nav": "chat",
            "conversations": conversations,
            "offset": offset,
            "page_size": PAGE_SIZE,
            "conversation": conversation,
            "messages": (conversation or {}).get("messages", []),
            "ready_documents": ready,
            "failed_count": failed,
            "processing_count": processing,
            "related": {},
            "sources": sources,
            "groups": groups,
            "scope": scope,
            "scope_names": names,
            "request_id": str(uuid4()),
            "draft": request.query_params.get("q", "")[:4000],
            "suggestions": SUGGESTIONS,
            **(extra or {}),
        },
        status_code=status_code,
    )


@router.get("/o/{org_id}/chat")
async def new_chat(request: Request, org_id: UUID):
    return await _chat_page(request, org_id)


@router.get("/o/{org_id}/chat/{conversation_id}")
async def show_chat(request: Request, org_id: UUID, conversation_id: UUID):
    return await _chat_page(request, org_id, conversation_id)


@router.post("/o/{org_id}/chat")
async def ask(request: Request, org_id: UUID):
    await workspace(request, org_id)
    form = await request.form()
    message = str(form.get("message") or "").strip()
    conversation_id = str(form.get("conversation_id") or "") or None
    try:
        request_id = str(UUID(str(form.get("request_id") or "")))
    except ValueError:
        request_id = str(uuid4())
    scope = scope_from(
        {
            "group_id": form.get("group_id") or None,
            "source_ids": form_list(form, "source_ids"),
            "document_ids": form_list(form, "document_ids"),
        }
    )
    target_id = UUID(conversation_id) if conversation_id else None
    if not message:
        return await _chat_page(
            request,
            org_id,
            target_id,
            {"error": "Type a question first.", "scope": scope},
            400,
        )
    try:
        result = await Backend(request).post(
            f"{api_org(org_id)}/chat",
            json={
                "message": message[:4000],
                "conversation_id": conversation_id,
                "request_id": request_id,
                **scope,
            },
        )
    except ApiError as error:
        # Keep the question and request id so retrying cannot create a duplicate turn.
        return await _chat_page(
            request,
            org_id,
            target_id,
            {
                "error": friendly_error(error),
                "draft": message,
                "request_id": request_id,
                "scope": scope,
            },
            200,
        )
    # Related passages are only in this response, so render the conversation
    # directly; the request id makes a browser resubmission return the same turn.
    conversation_id = UUID(str(result["conversation_id"]))
    response = await _chat_page(
        request,
        org_id,
        conversation_id,
        {
            "related": {
                str(result.get("message_id")): result.get("related_passages") or []
            }
        },
    )
    response.headers["HX-Push-Url"] = f"/o/{org_id}/chat/{conversation_id}#latest"
    return response


@router.post("/o/{org_id}/chat/{conversation_id}/rename")
async def rename_chat(
    request: Request,
    org_id: UUID,
    conversation_id: UUID,
    title: Annotated[str, Form(max_length=512)],
):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/chat/{conversation_id}",
        "Conversation renamed.",
        Backend(request).call(
            "PATCH",
            f"{api_org(org_id)}/conversations/{conversation_id}",
            json={"title": title},
        ),
    )


@router.post("/o/{org_id}/chat/{conversation_id}/delete")
async def delete_chat(request: Request, org_id: UUID, conversation_id: UUID):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/chat",
        "Conversation deleted.",
        Backend(request).call(
            "DELETE", f"{api_org(org_id)}/conversations/{conversation_id}"
        ),
    )


@router.post("/o/{org_id}/chat/{conversation_id}/messages/{message_id}/feedback")
async def feedback(
    request: Request,
    org_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    vote: Annotated[Literal["helpful", "problem"], Form()],
    reason: Annotated[str, Form()] = "",
    comment: Annotated[str, Form(max_length=1000)] = "",
):
    await workspace(request, org_id)
    return await act(
        request,
        f"/o/{org_id}/chat/{conversation_id}#message-{message_id}",
        "Thanks — your feedback was saved.",
        Backend(request).call(
            "PUT",
            f"{api_org(org_id)}/conversations/{conversation_id}/messages/{message_id}/feedback",
            json={
                "vote": vote,
                "reason": reason
                if reason in {"wrong_source", "missing_information", "incorrect_answer"}
                else None,
                "comment": comment.strip() or None,
            },
        ),
    )
