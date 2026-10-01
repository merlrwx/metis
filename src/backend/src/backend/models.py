import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_OBJECT = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Organisation(Timestamped, Base):
    __tablename__ = "organisations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class OrganisationMembership(Timestamped, Base):
    __tablename__ = "organisation_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')", name="ck_membership_role"
        ),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")


class Source(Timestamped, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("id", "organisation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="upload")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSON_OBJECT, nullable=False, default=dict
    )


class Document(Timestamped, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "organisation_id"),
        UniqueConstraint("organisation_id", "source_uri"),
        ForeignKeyConstraint(
            ["source_id", "organisation_id"],
            ["sources.id", "sources.organisation_id"],
            ondelete="CASCADE",
            name="fk_document_source_organisation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(2048))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DocumentVersion(Timestamped, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("id", "organisation_id"),
        UniqueConstraint("document_id", "checksum"),
        ForeignKeyConstraint(
            ["document_id", "organisation_id"],
            ["documents.id", "documents.organisation_id"],
            ondelete="CASCADE",
            name="fk_document_version_document_organisation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)


class IngestionJob(Timestamped, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'queued', 'processing', 'completed', 'failed')",
            name="ck_ingestion_job_status",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "organisation_id"],
            ["document_versions.id", "document_versions.organisation_id"],
            ondelete="CASCADE",
            name="fk_ingestion_job_version_organisation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(4096))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Timestamped, Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("id", "organisation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(
        String(512), nullable=False, default="New conversation"
    )


class Message(Timestamped, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('system', 'user', 'assistant')", name="ck_message_role"
        ),
        ForeignKeyConstraint(
            ["conversation_id", "organisation_id"],
            ["conversations.id", "conversations.organisation_id"],
            ondelete="CASCADE",
            name="fk_message_conversation_organisation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(nullable=False)
