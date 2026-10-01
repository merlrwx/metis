import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class OrganisationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


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
    created_at: datetime


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    source_id: uuid.UUID | None = None
    source_uri: str | None = Field(default=None, max_length=2048)


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


class SearchRequest(BaseModel):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    source_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    limit: int = Field(default=5, ge=1, le=20)


class SearchResultView(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_id: uuid.UUID | None
    source_name: str | None
    content: str
    page: int | None
    section: str | None
    score: float


class SearchView(BaseModel):
    embedding_model: str
    results: list[SearchResultView]


class ChatRequest(BaseModel):
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    conversation_id: uuid.UUID | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class CitationView(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_id: uuid.UUID | None
    source_name: str | None
    page: int | None
    section: str | None
    snippet: str


class ChatUsageView(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    answer: str
    citations: list[CitationView]
    model_id: str | None
    usage: ChatUsageView


class ConversationMessageView(BaseModel):
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
    id: uuid.UUID
    organisation_id: uuid.UUID
    title: str
    created_at: datetime
    messages: list[ConversationMessageView]
