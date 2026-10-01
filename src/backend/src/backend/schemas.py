import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
