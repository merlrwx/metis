import os
from urllib.parse import urlsplit
from uuid import uuid4

import streamlit as st
from api_client import ApiError, MetisApi

backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
api = MetisApi(backend_url)

st.set_page_config(page_title="Metis", page_icon="M", layout="wide")
st.markdown(
    """
    <style>
    :root {
      --metis-bg: oklch(1 0 0);
      --metis-sidebar: oklch(0.975 0.012 280);
      --metis-ink: oklch(0.22 0.025 280);
      --metis-muted: oklch(0.44 0.025 280);
      --metis-border: oklch(0.91 0.015 280);
      --metis-primary: oklch(0.43 0.09 280);
      --metis-accent: oklch(0.53 0.11 200);
    }
    [data-testid="stAppViewContainer"] { background: var(--metis-bg); color: var(--metis-ink); }
    [data-testid="stSidebar"] { background: var(--metis-sidebar); border-right: 1px solid var(--metis-border); }
    [data-testid="stHeader"] { background: transparent; }
    .main .block-container { max-width: 1280px; padding-top: 2rem; padding-bottom: 3rem; }
    h1, h2, h3 { color: var(--metis-ink); letter-spacing: -0.02em; }
    h1 { font-size: 2rem; font-weight: 650; }
    h2 { font-size: 1.45rem; font-weight: 620; }
    h3 { font-size: 1.1rem; font-weight: 600; }
    p, label, [data-testid="stCaptionContainer"] { color: var(--metis-ink); }
    [data-testid="stMarkdownContainer"] a { color: var(--metis-primary); }
    div[data-testid="stButton"] > button,
    div[data-testid="stFormSubmitButton"] > button {
      min-height: 2.75rem; border-radius: 0.4rem; border: 1px solid var(--metis-border);
      transition: background-color 180ms ease, border-color 180ms ease, color 180ms ease;
    }
    div[data-testid="stButton"] > button:hover,
    div[data-testid="stFormSubmitButton"] > button:hover { border-color: var(--metis-primary); color: var(--metis-primary); }
    div[data-testid="stButton"] > button:focus-visible,
    div[data-testid="stFormSubmitButton"] > button:focus-visible { outline: 3px solid var(--metis-accent); outline-offset: 2px; }
    button[kind="primary"] { background: var(--metis-primary); border-color: var(--metis-primary); color: white; }
    [data-testid="stFileUploader"] { border-radius: 0.4rem; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def clear_session() -> None:
    for key in (
        "metis_token",
        "metis_user",
        "metis_chat_org",
        "metis_chat_id",
        "metis_chat_messages",
        "metis_pending_chat",
        "metis_retry_text",
        "metis_chat_error",
        "active_organisation",
    ):
        st.session_state.pop(key, None)


def show_api_error(error: ApiError) -> None:
    st.error(error.detail)
    if error.status_code == 401 and st.session_state.get("metis_token"):
        clear_session()
        st.info("Your session expired. Sign in again to continue.")


def show_notice() -> None:
    message = st.session_state.pop("metis_notice", None)
    if message:
        st.success(message)


def rerun_with_notice(message: str) -> None:
    st.session_state["metis_notice"] = message
    st.rerun()


def render_login() -> None:
    st.title("Metis")
    st.write("Find answers in your organisation’s policies and procedures.")
    try:
        info = api.request("GET", "/api/info")
        if (
            not isinstance(info, dict)
            or info.get("name") != "Metis"
            or info.get("stage") != "grounded-chat"
        ):
            raise ApiError(502, "The configured service is not a Metis API.")
    except ApiError as error:
        st.error(error.detail)
    else:
        st.success("Metis API connected")

    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        st.subheader("Sign in")
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            try:
                st.session_state["metis_token"] = api.login(email, password)
                st.rerun()
            except (ApiError, KeyError) as error:
                show_api_error(
                    error
                    if isinstance(error, ApiError)
                    else ApiError(502, "The API did not return a sign-in token.")
                )

        with st.expander("Create an account"):
            with st.form("register_form"):
                name = st.text_input("Name", key="register_name")
                register_email = st.text_input("Email", key="register_email")
                register_password = st.text_input(
                    "Password", type="password", key="register_password"
                )
                st.caption("Use at least 12 characters.")
                register_submitted = st.form_submit_button("Create account")
            if register_submitted:
                try:
                    api.register(register_email, name, register_password)
                    st.session_state["metis_token"] = api.login(
                        register_email, register_password
                    )
                    st.rerun()
                except ApiError as error:
                    show_api_error(error)


def create_organisation(token: str) -> None:
    st.title("Create your organisation")
    st.write(
        "Your organisation keeps its documents and conversations private to its members."
    )
    with st.form("create_organisation_form"):
        name = st.text_input("Organisation name", key="new_organisation_name")
        submitted = st.form_submit_button("Create organisation", type="primary")
    if submitted:
        try:
            api.request(
                "POST",
                "/api/organisations",
                token=token,
                payload={"name": name},
            )
            rerun_with_notice("Organisation created. You are its owner.")
        except ApiError as error:
            show_api_error(error)


def request_json(method: str, path: str, token: str, payload: dict | None = None):
    try:
        return api.request(method, path, token=token, payload=payload)
    except ApiError as error:
        show_api_error(error)
        return None


def status_label(value: str | None) -> str:
    if value in {"indexed", "completed"}:
        return "Ready"
    if value in {"pending", "queued", "processing"}:
        return "Processing"
    if value == "failed":
        return "Failed"
    return "Metadata only" if value is None else "Not indexed"


def document_rows(documents: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "Document": document["title"],
            "Status": status_label(document.get("ingestion_status")),
            "Updated": document.get("updated_at", "")[:19].replace("T", " "),
        }
        for document in documents
    ]


def render_dashboard(organisation_id: str, token: str, documents: list[dict]) -> None:
    st.title("Dashboard")
    total = len(documents)
    processing = sum(
        document.get("ingestion_status") in {"pending", "queued", "processing"}
        for document in documents
    )
    failed = [
        document
        for document in documents
        if document.get("ingestion_status") == "failed"
    ]

    counts = st.columns(3)
    for column, label, value in zip(
        counts,
        ("Documents", "Processing", "Failed jobs"),
        (total, processing, len(failed)),
        strict=True,
    ):
        with column:
            st.caption(label)
            st.markdown(f"**{value}**")

    if processing:
        st.info(f"{processing} document(s) are still processing.")
    if st.button("Refresh status", key="dashboard_refresh"):
        st.rerun()

    st.subheader("Recent ingestion")
    recent = sorted(
        documents,
        key=lambda document: document.get("updated_at", ""),
        reverse=True,
    )[:5]
    if recent:
        st.dataframe(document_rows(recent), hide_index=True, use_container_width=True)
    else:
        st.info(
            "No documents yet. Upload a policy or procedure from Knowledge to start."
        )

    if failed:
        st.subheader("Failed jobs")
        for document in failed:
            row, action = st.columns([5, 1])
            with row:
                st.write(document["title"])
                if document.get("ingestion_error"):
                    st.caption(document["ingestion_error"])
            with action:
                if st.button("Retry", key=f"dashboard_retry_{document['id']}"):
                    retry_ingestion(organisation_id, document["id"], token)


def retry_ingestion(organisation_id: str, document_id: str, token: str) -> None:
    try:
        api.request(
            "POST",
            f"/api/organisations/{organisation_id}/documents/{document_id}/retry",
            token=token,
        )
    except ApiError as error:
        show_api_error(error)
    else:
        rerun_with_notice("Ingestion queued again.")


def render_knowledge(
    organisation_id: str, token: str, role: str, documents: list[dict]
) -> None:
    st.title("Knowledge")
    sources = request_json(
        "GET", f"/api/organisations/{organisation_id}/sources", token
    )
    if sources is None:
        return

    with st.expander("Saved knowledge groups"):
        groups = request_json(
            "GET", f"/api/organisations/{organisation_id}/groups", token
        )
        if groups is None:
            return
        for group in groups:
            st.write(group["name"])
            st.caption(f"{len(group['source_ids'])} sources")
            if role in {"owner", "admin"}:
                with st.form(f"edit_group_{group['id']}"):
                    group_name = st.text_input("Group name", value=group["name"])
                    selected = st.multiselect(
                        "Group sources",
                        [item["id"] for item in sources],
                        default=group["source_ids"],
                        format_func=lambda value: next(
                            item["name"] for item in sources if item["id"] == value
                        ),
                    )
                    save = st.form_submit_button("Save group")
                    remove = st.form_submit_button("Delete group")
                if save or remove:
                    try:
                        api.request(
                            "DELETE" if remove else "PUT",
                            f"/api/organisations/{organisation_id}/groups/{group['id']}",
                            token=token,
                            payload=None
                            if remove
                            else {"name": group_name, "source_ids": selected},
                        )
                        rerun_with_notice(
                            "Group deleted." if remove else "Group saved."
                        )
                    except ApiError as error:
                        show_api_error(error)
        if role in {"owner", "admin"}:
            with st.form("create_group"):
                name = st.text_input("New group name")
                selected = st.multiselect(
                    "Include sources",
                    [item["id"] for item in sources],
                    format_func=lambda value: next(
                        item["name"] for item in sources if item["id"] == value
                    ),
                )
                create = st.form_submit_button("Create group")
            if create:
                try:
                    api.request(
                        "POST",
                        f"/api/organisations/{organisation_id}/groups",
                        token=token,
                        payload={"name": name, "source_ids": selected},
                    )
                    rerun_with_notice("Group created.")
                except ApiError as error:
                    show_api_error(error)
        st.caption(
            "Groups save a source selection. They do not change who can access knowledge."
        )

    source_column, detail_column = st.columns([1, 2])
    with source_column:
        st.subheader("Sources")
        if sources:
            for source in sources:
                st.write(source["name"])
                st.caption(
                    "Microsoft 365"
                    if source["type"] == "microsoft365"
                    else source["type"].replace("_", " ").title()
                )
                if source["type"] == "microsoft365":
                    status = source.get("sync_status", "idle")
                    label = {
                        "queued": "Queued for synchronization",
                        "syncing": "Synchronizing library",
                        "failed": "Last synchronization failed",
                    }.get(
                        status,
                        "Last synchronized: " + source["last_synced_at"]
                        if source.get("last_synced_at")
                        else "Not yet synchronized",
                    )
                    st.caption(label)
                    if status == "failed":
                        st.caption(
                            "Ask your administrator to check the library connection, then retry."
                        )
                    if role in {"owner", "admin"} and st.button(
                        "Sync now", key=f"sync_source_{source['id']}"
                    ):
                        try:
                            result = api.request(
                                "POST",
                                f"/api/organisations/{organisation_id}/sources/{source['id']}/sync",
                                token=token,
                            )
                            rerun_with_notice(
                                "Library synchronization is in progress."
                                if result.get("sync_status") == "syncing"
                                else "Library synchronization queued. Refresh to check its progress."
                            )
                        except ApiError as error:
                            show_api_error(error)
        else:
            st.info("Uploaded files will appear here as a source.")
        if role in {"owner", "admin"}:
            source_kind = st.selectbox(
                "Source type",
                ["File uploads", "Microsoft 365 library"],
                key="new_source_type",
            )
            with st.form("create_source_form"):
                source_name = st.text_input("New source name")
                auto_sync = False
                if source_kind == "Microsoft 365 library":
                    st.caption(
                        "Your administrator must connect a library approved for this whole organisation before its first sync."
                    )
                    auto_sync = st.checkbox(
                        "Synchronize automatically every 15 minutes", value=False
                    )
                source_submitted = st.form_submit_button("Add source")
            if source_submitted:
                try:
                    api.request(
                        "POST",
                        f"/api/organisations/{organisation_id}/sources",
                        token=token,
                        payload={
                            "name": source_name,
                            "type": "microsoft365"
                            if source_kind == "Microsoft 365 library"
                            else "upload",
                            "configuration": {"sync_enabled": auto_sync}
                            if source_kind == "Microsoft 365 library"
                            else {},
                        },
                    )
                    rerun_with_notice("Source added.")
                except ApiError as error:
                    show_api_error(error)

    with detail_column:
        st.subheader("Documents")
        if documents:
            st.dataframe(
                document_rows(documents), hide_index=True, use_container_width=True
            )
        else:
            st.info(
                "Your knowledge base is empty. Upload a PDF, DOCX, text, or Markdown file."
            )

        for document in documents:
            if document.get("ingestion_status") != "deleted" and st.button(
                f"View details · {document['title']}", key=f"detail_{document['id']}"
            ):
                st.session_state[f"document_detail_{organisation_id}"] = document["id"]
        render_document_detail(organisation_id, token, role)

        failed = [
            document
            for document in documents
            if document.get("ingestion_status") == "failed"
        ]
        for document in failed:
            retry_col, error_col = st.columns([1, 4])
            with retry_col:
                if role in {"owner", "admin"} and st.button(
                    "Retry ingestion", key=f"knowledge_retry_{document['id']}"
                ):
                    retry_ingestion(organisation_id, document["id"], token)
            with error_col:
                st.caption(document.get("ingestion_error") or "Ingestion failed")

    if role in {"owner", "admin"}:
        st.subheader("Upload a document")
        st.caption(
            "Uploads are shared with all members of this organisation. "
            "Ready means searchable; it does not guarantee an answer to every question."
        )
        source_options = {None: "File uploads"}
        for source in sources:
            if source["type"] != "upload":
                continue
            label = source["name"]
            if label in source_options.values():
                label = f"{label} · {source['id'][:8]}"
            source_options[source["id"]] = label
        with st.form("upload_form"):
            selected_source = st.selectbox(
                "Source",
                list(source_options),
                format_func=source_options.__getitem__,
            )
            conflict_action = st.selectbox(
                "If this filename already exists",
                ["reject", "replace", "new"],
                format_func={
                    "reject": "Ask me to choose",
                    "replace": "Replace the existing document",
                    "new": "Create a separate document",
                }.__getitem__,
            )
            uploaded_files = st.file_uploader(
                "Choose a PDF, DOCX, TXT, Markdown, or CSV file",
                type=["pdf", "docx", "txt", "md", "markdown", "csv"],
                accept_multiple_files=True,
                help="Upload up to 10 files at a time. Each file is processed independently.",
            )
            upload_submitted = st.form_submit_button(
                "Upload and process", type="primary"
            )
        if upload_submitted:
            if not uploaded_files:
                st.error("Choose a document before uploading.")
            elif len(uploaded_files) > 10:
                st.error("Choose at most 10 files for one batch.")
            else:
                results = []
                for uploaded_file in uploaded_files:
                    try:
                        with st.spinner(f"Uploading {uploaded_file.name}…"):
                            result = api.request(
                                "POST",
                                f"/api/organisations/{organisation_id}/documents/upload",
                                token=token,
                                upload=(
                                    uploaded_file.name,
                                    uploaded_file.getvalue(),
                                    uploaded_file.type or "application/octet-stream",
                                ),
                                source_id=selected_source,
                                conflict_action=conflict_action,
                            )
                        results.append(
                            {
                                "ok": True,
                                "message": f"{result['document']['title']} is {status_label(result['job']['status']).lower()}.",
                            }
                        )
                    except ApiError as error:
                        results.append(
                            {
                                "ok": False,
                                "message": f"{uploaded_file.name}: {error.detail}. Select this file again to retry.",
                            }
                        )
                st.session_state[f"upload_results_{organisation_id}"] = results
                st.rerun()
        for result in st.session_state.get(f"upload_results_{organisation_id}", []):
            if result["ok"]:
                st.success(result["message"])
            else:
                st.error(result["message"])
    else:
        st.info("An organisation owner or admin can add sources and upload documents.")


def render_document_detail(organisation_id: str, token: str, role: str) -> None:
    document_id = st.session_state.get(f"document_detail_{organisation_id}")
    if not document_id:
        return
    path = f"/api/organisations/{organisation_id}/documents/{document_id}"
    try:
        detail = api.request("GET", path + "/detail", token=token)
        document = detail["document"]
        with st.expander(f"Document details · {document['title']}", expanded=True):
            st.caption(detail["visibility"])
            st.caption(detail["readiness"])
            st.write(f"Status: {status_label(document['ingestion_status'])}")
            st.write(f"Pages: {detail['page_count']} · Chunks: {detail['chunk_count']}")
            st.caption(
                f"Source: {document['source_id']} · Version: {document['current_version_id']}"
            )
            st.caption(
                f"Last successful indexing: {detail['last_successful_indexing'] or 'Not indexed yet'}"
            )
            if detail.get("versions"):
                st.caption(
                    "Stored versions · only the current version is used for new answers."
                )
                st.dataframe(
                    detail["versions"], hide_index=True, use_container_width=True
                )
                if detail.get("versions_truncated"):
                    st.caption("Showing the latest 50 stored versions.")
            st.text(
                detail["extraction_preview"] or "No extracted text is available yet."
            )
            if detail["preview_truncated"]:
                st.caption("Preview shows the first 4,000 characters.")
            if role in {"owner", "admin"}:
                with st.form(f"replace_document_{document_id}"):
                    replacement = st.file_uploader(
                        "Replace with a new version",
                        type=["pdf", "docx", "txt", "md", "markdown", "csv"],
                        key=f"replacement_{document_id}",
                    )
                    st.caption(
                        "Replaces this document even if the filename differs. Previous versions remain stored but are excluded from new answers."
                    )
                    if st.form_submit_button("Replace document"):
                        if replacement is None:
                            st.error("Choose a replacement file.")
                        else:
                            api.request(
                                "POST",
                                f"/api/organisations/{organisation_id}/documents/upload",
                                token=token,
                                upload=(
                                    replacement.name,
                                    replacement.getvalue(),
                                    replacement.type or "application/octet-stream",
                                ),
                                conflict_action="replace",
                                replace_document_id=document_id,
                            )
                            rerun_with_notice(
                                "Replacement queued; previous version is excluded from new answers."
                            )
                with st.form(f"rename_document_{document_id}"):
                    title = st.text_input("Document title", value=document["title"])
                    if st.form_submit_button("Rename document"):
                        api.request(
                            "PATCH", path, token=token, payload={"title": title}
                        )
                        rerun_with_notice("Document renamed.")
                if st.button(
                    "Re-index document", key=f"reindex_document_{document_id}"
                ):
                    api.request("POST", path + "/reindex", token=token)
                    rerun_with_notice("Document queued for re-indexing.")
                st.caption(
                    "Remove excludes this document from answers, previews and downloads. "
                    "Original files, historical versions and saved answer data remain in storage. "
                    "This is not permanent erasure."
                )
                confirmed = st.checkbox(
                    "Remove this document from the library",
                    key=f"remove_confirm_{document_id}",
                )
                if st.button(
                    "Remove document",
                    disabled=not confirmed,
                    key=f"remove_document_{document_id}",
                ):
                    api.request("DELETE", path, token=token)
                    st.session_state.pop(f"document_detail_{organisation_id}", None)
                    rerun_with_notice(
                        "Document removed from the library. Stored history is retained."
                    )
    except ApiError as error:
        show_api_error(error)


def render_citations(
    citations: list[dict],
    organisation_id: str | None = None,
    token: str | None = None,
    key_prefix: str = "",
) -> None:
    if not citations:
        st.caption("No supporting document sources were returned.")
        return
    st.caption(f"Sources · {len(citations)}")
    for index, citation in enumerate(citations, start=1):
        title = citation.get("document_title", "Source document")
        with st.expander(f"[C{index}] {title}"):
            location = []
            if citation.get("page") is not None:
                location.append(f"Page {citation['page']}")
            if citation.get("section"):
                location.append(citation["section"])
            if citation.get("source_name"):
                location.insert(0, citation["source_name"])
            if location:
                st.caption(" · ".join(location))
            if citation.get("document_version_id"):
                st.caption("Version · " + citation["document_version_id"])
            if citation.get("source_modified_at"):
                st.caption("Source modified · " + citation["source_modified_at"])
            st.write(citation.get("snippet", ""))
            version = citation.get("document_version_id")
            if (
                organisation_id
                and token
                and version
                and st.button(
                    "Prepare original download",
                    key=f"{key_prefix}_original_{citation['chunk_id']}_{index}",
                )
            ):
                try:
                    original = api.request(
                        "GET",
                        f"/api/organisations/{organisation_id}/documents/{citation['document_id']}/versions/{version}/download",
                        token=token,
                        raw=True,
                    )
                    st.download_button(
                        "Download original",
                        original,
                        file_name=title,
                        key=f"{key_prefix}_download_{citation['chunk_id']}_{index}",
                    )
                except ApiError as error:
                    show_api_error(error)
            source_url = citation.get("source_url")
            try:
                source_link = urlsplit(source_url or "")
            except ValueError:
                source_link = urlsplit("")
            if source_link.scheme in {"http", "https"} and source_link.netloc:
                st.link_button("Open source document", source_url)


def render_chat(organisation_id: str, token: str) -> None:
    st.title("Chat")
    if st.session_state.get("metis_chat_org") != organisation_id:
        st.session_state["metis_chat_org"] = organisation_id
        st.session_state.pop("metis_pending_chat", None)
        st.session_state.pop("metis_retry_text", None)
        st.session_state.pop("metis_chat_error", None)
        st.session_state["metis_chat_id"] = None
        st.session_state["metis_chat_messages"] = []
    if st.button("New conversation", key="new_conversation"):
        st.session_state["metis_chat_id"] = None
        st.session_state["metis_chat_messages"] = []
        st.session_state.pop("metis_pending_chat", None)
        for suffix in ("document", "group", "sources", "documents"):
            st.session_state.pop(f"chat_{suffix}_{organisation_id}", None)
        st.rerun()

    current_conversation = st.session_state.get("metis_chat_id")
    if current_conversation:
        refreshed = request_json(
            "GET",
            f"/api/organisations/{organisation_id}/conversations/{current_conversation}",
            token,
        )
        if refreshed is None:
            st.session_state["metis_chat_messages"] = []
            return
        st.session_state["metis_chat_messages"] = refreshed["messages"]
    st.caption(
        "Conversations are visible to their creator and organisation owners/admins."
    )
    with st.expander("Saved conversations"):
        offset = st.session_state.get(f"conversation_offset_{organisation_id}", 0)
        saved = request_json(
            "GET",
            f"/api/organisations/{organisation_id}/conversations?limit=20&offset={offset}",
            token,
        )
        if saved is None:
            return
        titles = {item["id"]: item["title"] for item in saved}
        selected = st.selectbox(
            "Conversation",
            [None, *titles],
            format_func=lambda value: (
                "Choose a conversation" if value is None else titles[value]
            ),
            key=f"saved_conversation_{organisation_id}",
        )
        if selected is not None:
            if st.button("Open conversation"):
                restored = request_json(
                    "GET",
                    f"/api/organisations/{organisation_id}/conversations/{selected}",
                    token,
                )
                if restored is not None:
                    st.session_state["metis_chat_id"] = restored["id"]
                    st.session_state["metis_chat_messages"] = restored["messages"]
                    st.session_state.pop("metis_pending_chat", None)
                    restored_scope = restored.get("scope", {})
                    for suffix, value in (
                        ("document", restored_scope.get("document_id")),
                        ("group", restored_scope.get("group_id")),
                        ("sources", restored_scope.get("source_ids", [])),
                        ("documents", restored_scope.get("document_ids", [])),
                    ):
                        st.session_state[f"chat_{suffix}_{organisation_id}"] = value
                    st.rerun()
            with st.form("rename_conversation"):
                title = st.text_input("Conversation name", value=titles[selected])
                rename = st.form_submit_button("Rename conversation")
                remove = st.form_submit_button("Delete conversation")
            if rename or remove:
                try:
                    api.request(
                        "DELETE" if remove else "PATCH",
                        f"/api/organisations/{organisation_id}/conversations/{selected}",
                        token=token,
                        payload=None if remove else {"title": title},
                    )
                    if remove and st.session_state.get("metis_chat_id") == selected:
                        st.session_state["metis_chat_id"] = None
                        st.session_state["metis_chat_messages"] = []
                    rerun_with_notice(
                        "Conversation deleted." if remove else "Conversation renamed."
                    )
                except ApiError as error:
                    show_api_error(error)
        previous, following = st.columns(2)
        if previous.button("Previous conversations", disabled=offset == 0):
            st.session_state[f"conversation_offset_{organisation_id}"] = max(
                0, offset - 20
            )
            st.rerun()
        if following.button("Next conversations", disabled=len(saved) < 20):
            st.session_state[f"conversation_offset_{organisation_id}"] = offset + 20
            st.rerun()

    st.write("Ask a question about this organisation’s indexed knowledge.")
    documents = request_json(
        "GET", f"/api/organisations/{organisation_id}/documents", token
    )
    if documents is None:
        return
    ready_documents = {
        item["id"]: item
        for item in documents
        if item.get("ingestion_status") == "indexed"
    }
    for identifier in st.session_state.get(f"chat_documents_{organisation_id}", []):
        ready_documents.setdefault(
            identifier,
            {"title": "Unavailable document — remove this filter to continue"},
        )
    document_id = st.selectbox(
        "Answer from",
        [None, *ready_documents],
        format_func=lambda value: (
            "All indexed documents"
            if value is None
            else ready_documents[value]["title"]
        ),
        key=f"chat_document_{organisation_id}",
        help="Choose a document to ask directly about its contents, such as a payslip's net payment.",
    )
    if document_id is not None:
        st.caption(
            "Answers will use only this document. Start a new conversation when changing topics."
        )
    else:
        st.caption(
            "Metis finds relevant evidence across all indexed knowledge. Filters are optional."
        )
    scope = {"document_id": document_id}
    with st.expander("Narrow knowledge scope"):
        sources = request_json(
            "GET", f"/api/organisations/{organisation_id}/sources", token
        )
        groups = request_json(
            "GET", f"/api/organisations/{organisation_id}/groups", token
        )
        if sources is None or groups is None:
            return
        source_names = {item["id"]: item["name"] for item in sources}
        group_names = {item["id"]: item["name"] for item in groups}
        previous_group = st.session_state.get(f"chat_group_{organisation_id}")
        if previous_group is not None:
            group_names.setdefault(
                previous_group, "Unavailable group — clear this filter to continue"
            )
        for identifier in st.session_state.get(f"chat_sources_{organisation_id}", []):
            source_names.setdefault(
                identifier, "Unavailable source — remove this filter to continue"
            )
        group_id = st.selectbox(
            "Saved group",
            [None, *group_names],
            format_func=lambda value: (
                "No group" if value is None else group_names[value]
            ),
            key=f"chat_group_{organisation_id}",
        )
        selected_sources = st.multiselect(
            "Sources",
            list(source_names),
            format_func=source_names.get,
            disabled=group_id is not None,
            key=f"chat_sources_{organisation_id}",
        )
        selected_documents = st.multiselect(
            "Documents",
            list(ready_documents),
            format_func=lambda value: ready_documents[value]["title"],
            key=f"chat_documents_{organisation_id}",
        )
        if group_id is not None:
            scope["group_id"] = group_id
        elif selected_sources:
            scope["source_ids"] = selected_sources
        if selected_documents:
            scope.pop("document_id", None)
            scope["document_ids"] = selected_documents
        st.caption(
            "Filters narrow the search. With no filters, Metis searches all indexed knowledge."
        )
    scope_labels = []
    if group_id is not None:
        scope_labels.append("Group: " + group_names[group_id])
    elif selected_sources:
        scope_labels.append(
            "Sources: " + ", ".join(source_names[value] for value in selected_sources)
        )
    chosen_documents = selected_documents or ([document_id] if document_id else [])
    if chosen_documents:
        scope_labels.append(
            "Documents: "
            + ", ".join(ready_documents[value]["title"] for value in chosen_documents)
        )
    st.caption(
        "Current scope · "
        + (" · ".join(scope_labels) if scope_labels else "All indexed knowledge")
    )
    for message_index, message in enumerate(
        st.session_state.get("metis_chat_messages", [])
    ):
        with st.chat_message(message["role"]):
            outcome_label = {
                "partially_answered": "Partially answered",
                "clarification_needed": "Needs clarification",
                "insufficient_evidence": "No supporting evidence",
            }.get(message.get("outcome"))
            if outcome_label:
                st.caption(outcome_label)
            st.markdown(message["content"])
            if message.get("citations") is not None:
                render_citations(
                    message["citations"],
                    organisation_id,
                    token,
                    key_prefix=f"chat_{message_index}",
                )

            if message.get("id") and message["role"] == "assistant":
                with st.form(f"feedback_{message['id']}"):
                    vote = st.selectbox(
                        "Was this helpful?",
                        ["helpful", "problem"],
                        format_func=lambda value: (
                            "Helpful" if value == "helpful" else "Something is wrong"
                        ),
                    )
                    reason = st.selectbox(
                        "Reason",
                        [
                            None,
                            "wrong_source",
                            "missing_information",
                            "incorrect_answer",
                        ],
                        format_func=lambda value: (
                            "Optional"
                            if value is None
                            else value.replace("_", " ").title()
                        ),
                    )
                    comment = st.text_input("Optional feedback", max_chars=1000)
                    feedback_sent = st.form_submit_button("Send feedback")
                if feedback_sent:
                    try:
                        api.request(
                            "PUT",
                            f"/api/organisations/{organisation_id}/conversations/{st.session_state['metis_chat_id']}/messages/{message['id']}/feedback",
                            token=token,
                            payload={
                                "vote": vote,
                                "reason": reason,
                                "comment": comment or None,
                            },
                        )
                        st.success("Feedback saved.")
                    except ApiError as error:
                        show_api_error(error)
    pending = st.session_state.get("metis_pending_chat")
    retry = False
    if pending:
        if st.session_state.get("metis_chat_error"):
            st.error(st.session_state["metis_chat_error"])
        retry_text = st.text_area(
            "Question to retry", value=pending["message"], key="metis_retry_text"
        )
        retry = st.button("Retry question")
    prompt = st.chat_input("Ask about your documents")
    if retry:
        prompt = retry_text
        if prompt != pending["message"]:
            pending = {**pending, "message": prompt, "request_id": str(uuid4())}

    if prompt:
        if not retry:
            pending = {
                "message": prompt,
                "conversation_id": st.session_state.get("metis_chat_id"),
                "request_id": str(uuid4()),
                **scope,
            }
        st.session_state["metis_pending_chat"] = pending
        try:
            with st.spinner("Searching knowledge and preparing an answer…"):
                response = api.request(
                    "POST",
                    f"/api/organisations/{organisation_id}/chat",
                    token=token,
                    payload=pending,
                )
        except ApiError as error:
            if error.status_code == 401:
                show_api_error(error)
            else:
                st.session_state["metis_chat_error"] = error.detail
                st.rerun()
        else:
            st.session_state.pop("metis_pending_chat", None)
            st.session_state.pop("metis_chat_error", None)
            st.session_state.pop("metis_retry_text", None)
            st.session_state["metis_chat_id"] = response["conversation_id"]
            st.session_state.setdefault("metis_chat_messages", []).extend(
                [
                    {"role": "user", "content": prompt},
                    {
                        "role": "assistant",
                        "id": response.get("message_id"),
                        "content": response["answer"],
                        "outcome": response.get("outcome", "answered"),
                        "citations": response.get("citations", []),
                    },
                ]
            )
            st.rerun()


def render_settings(organisation: dict, role: str, token: str) -> None:
    st.title("Settings")
    st.subheader("Organisation")
    st.write(organisation["name"])
    st.caption(f"Organisation ID · {organisation['id']}")

    if role not in {"owner", "admin"}:
        st.info("An owner or admin can manage organisation members.")
        return

    members = request_json(
        "GET", f"/api/organisations/{organisation['id']}/members", token
    )
    if members is None:
        return
    st.subheader("Members")
    st.dataframe(
        [
            {"Name": member["name"], "Email": member["email"], "Role": member["role"]}
            for member in members
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Add a member")
    st.caption("The person must create a Metis account before you add them.")
    role_options = ["member", "admin"] if role == "owner" else ["member"]
    with st.form("add_member_form"):
        member_email = st.text_input("Member email")
        member_role = st.selectbox("Role", role_options)
        submitted = st.form_submit_button("Add member", type="primary")
    if submitted:
        try:
            api.request(
                "POST",
                f"/api/organisations/{organisation['id']}/members",
                token=token,
                payload={"email": member_email, "role": member_role},
            )
            rerun_with_notice("Member added.")
        except ApiError as error:
            show_api_error(error)


def main() -> None:
    show_notice()
    token = st.session_state.get("metis_token")
    if not token:
        render_login()
        return

    try:
        user = api.request("GET", "/api/auth/me", token=token)
    except ApiError as error:
        if error.status_code == 401:
            clear_session()
            st.warning("Your session expired. Sign in again to continue.")
            render_login()
        else:
            show_api_error(error)
        return

    st.session_state["metis_user"] = user
    memberships = user.get("memberships", [])
    if not memberships:
        create_organisation(token)
        return

    membership_by_id = {
        membership["organisation_id"]: membership for membership in memberships
    }
    st.sidebar.title("Metis")
    st.sidebar.caption("Organisation knowledge")
    organisation_id = st.sidebar.selectbox(
        "Organisation",
        list(membership_by_id),
        format_func=lambda value: membership_by_id[value]["organisation_name"],
        key=f"selected_organisation_{user['id']}",
    )
    membership = membership_by_id[organisation_id]
    st.sidebar.caption(f"{user['email']} · {membership['role']}")
    page = st.sidebar.radio("Workspace", ["Dashboard", "Knowledge", "Chat", "Settings"])
    if st.sidebar.button("Sign out", key="sign_out"):
        clear_session()
        st.rerun()

    if st.session_state.get("active_organisation") != organisation_id:
        st.session_state["active_organisation"] = organisation_id
        st.session_state["metis_chat_org"] = None
        st.session_state["metis_chat_id"] = None
        st.session_state["metis_chat_messages"] = []

    organisation = {
        "id": organisation_id,
        "name": membership["organisation_name"],
    }
    documents = request_json(
        "GET", f"/api/organisations/{organisation_id}/documents", token
    )
    if documents is None:
        return

    if page == "Dashboard":
        render_dashboard(organisation_id, token, documents)
    elif page == "Knowledge":
        render_knowledge(organisation_id, token, membership["role"], documents)
    elif page == "Chat":
        render_chat(organisation_id, token)
    else:
        render_settings(organisation, membership["role"], token)


main()
