import os
from urllib.parse import urlsplit

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
            uploaded_file = st.file_uploader(
                "Choose a PDF, DOCX, TXT, or Markdown file",
                type=["pdf", "docx", "txt", "md", "markdown"],
            )
            upload_submitted = st.form_submit_button(
                "Upload and process", type="primary"
            )
        if upload_submitted:
            if uploaded_file is None:
                st.error("Choose a document before uploading.")
            else:
                try:
                    with st.spinner("Uploading document and starting ingestion…"):
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
                        )
                    rerun_with_notice(
                        f"{result['document']['title']} is {status_label(result['job']['status']).lower()}."
                    )
                except ApiError as error:
                    show_api_error(error)
    else:
        st.info("An organisation owner or admin can add sources and upload documents.")


def render_citations(citations: list[dict]) -> None:
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
            st.write(citation.get("snippet", ""))
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
        st.session_state["metis_chat_id"] = None
        st.session_state["metis_chat_messages"] = []
    if st.button("New conversation", key="new_conversation"):
        st.session_state["metis_chat_id"] = None
        st.session_state["metis_chat_messages"] = []
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
            "For a question about one file, select it above to use its contents directly."
        )
    for message in st.session_state.get("metis_chat_messages", []):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("citations") is not None:
                render_citations(message["citations"])

    prompt = st.chat_input("Ask about your documents")
    if prompt:
        try:
            with st.spinner("Searching your organisation’s knowledge…"):
                response = api.request(
                    "POST",
                    f"/api/organisations/{organisation_id}/chat",
                    token=token,
                    payload={
                        "message": prompt,
                        "conversation_id": st.session_state.get("metis_chat_id"),
                        "document_id": document_id,
                    },
                )
        except ApiError as error:
            show_api_error(error)
        else:
            st.session_state["metis_chat_id"] = response["conversation_id"]
            st.session_state.setdefault("metis_chat_messages", []).extend(
                [
                    {"role": "user", "content": prompt},
                    {
                        "role": "assistant",
                        "content": response["answer"],
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
