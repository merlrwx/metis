import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class UserRegister(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        email = value.strip().casefold()
        if email.count("@") != 1 or any(character.isspace() for character in email):
            raise ValueError("Enter a valid email address")
        return email

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Name cannot be blank")
        return name


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    created_at: datetime


class TokenView(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MembershipView(BaseModel):
    organisation_id: uuid.UUID
    organisation_name: str
    role: str


class CurrentUserView(UserView):
    memberships: list[MembershipView]


class MemberAdd(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="member", pattern="^(admin|member)$")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        email = value.strip().casefold()
        if email.count("@") != 1 or any(character.isspace() for character in email):
            raise ValueError("Enter a valid email address")
        return email


class OrganisationMemberView(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: str
    created_at: datetime


class AuditEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: uuid.UUID | None
    details: dict[str, Any]
    created_at: datetime


class OrganisationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Organisation name cannot be blank")
        return name


class OrganisationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(default="upload", min_length=1, max_length=32)
    configuration: dict[str, Any] = Field(default_factory=dict)


class SourceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    type: str
    name: str
    configuration: dict[str, Any]
    sync_status: str = "idle"
    sync_started_at: datetime | None = None
    last_synced_at: datetime | None = None
    sync_error: str | None = None
    created_at: datetime


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    source_id: uuid.UUID | None = None
    source_uri: str | None = Field(default=None, max_length=2048)


class DocumentRename(BaseModel):
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
    ]


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    source_id: uuid.UUID | None
    title: str
    source_uri: str | None
    current_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    ingestion_status: str | None = None
    ingestion_error: str | None = None


class TestJobCreate(BaseModel):
    organisation_id: uuid.UUID


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    document_version_id: uuid.UUID | None
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class UploadView(BaseModel):
    document: DocumentView
    job: JobView


class KnowledgeGroupWrite(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    source_ids: list[uuid.UUID] = Field(max_length=20)


class KnowledgeGroupView(KnowledgeGroupWrite):
    id: uuid.UUID


class KnowledgeScope(BaseModel):
    group_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    source_ids: list[uuid.UUID] | None = Field(
        default=None, min_length=1, max_length=20
    )
    document_ids: list[uuid.UUID] | None = Field(
        default=None, min_length=1, max_length=20
    )


class SearchRequest(KnowledgeScope):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    limit: int = Field(default=5, ge=1, le=20)


class SearchResultView(BaseModel):
    document_version_id: uuid.UUID | None = None
    source_modified_at: datetime | None = None
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_url: str | None = None
    source_id: uuid.UUID | None
    source_name: str | None
    content: str
    page: int | None
    section: str | None
    score: float


class SearchView(BaseModel):
    embedding_model: str
    results: list[SearchResultView]


class ChatRequest(KnowledgeScope):
    request_id: uuid.UUID | None = None
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    conversation_id: uuid.UUID | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class CitationView(BaseModel):
    document_version_id: uuid.UUID | None = None
    source_modified_at: datetime | None = None
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_url: str | None = None
    source_id: uuid.UUID | None
    source_name: str | None
    page: int | None
    section: str | None
    snippet: str


class ChatUsageView(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class ChatResponse(BaseModel):
    message_id: uuid.UUID | None = None
    outcome: Literal[
        "answered",
        "partially_answered",
        "clarification_needed",
        "insufficient_evidence",
    ] = "answered"
    conversation_id: uuid.UUID
    answer: str
    citations: list[CitationView]
    model_id: str | None
    usage: ChatUsageView


class ConversationMessageView(BaseModel):
    outcome: str | None = None
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: list[CitationView]
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime


class ConversationView(BaseModel):
    scope: dict[str, Any] = Field(default_factory=dict)
    id: uuid.UUID
    organisation_id: uuid.UUID
    title: str
    created_at: datetime
    messages: list[ConversationMessageView]


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    created_at: datetime
    scope: dict[str, Any]


class ConversationRename(BaseModel):
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
    ]


class FeedbackWrite(BaseModel):
    vote: Literal["helpful", "problem"]
    reason: (
        Literal["wrong_source", "missing_information", "incorrect_answer"] | None
    ) = None
    comment: str | None = Field(default=None, max_length=1000)
