"""add tenant-scoped vector chunks

Revision ID: d42f6ed4ca17
Revises: 65d4fe28c6a1
Create Date: 2026-10-01 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "d42f6ed4ca17"
down_revision: str | None = "65d4fe28c6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    metadata_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=512), nullable=True),
        sa.Column("metadata", metadata_type, nullable=False),
        sa.Column("embedding_model", sa.String(length=255), nullable=False),
        sa.Column("embedding", VECTOR(1536), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "organisation_id"],
            ["document_versions.id", "document_versions.organisation_id"],
            name="fk_chunk_version_organisation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id", "chunk_index", name="uq_chunk_version_index"
        ),
    )
    op.create_index(
        "ix_chunks_organisation_id_document_version_id",
        "chunks",
        ["organisation_id", "document_version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_organisation_id_document_version_id", table_name="chunks")
    op.drop_table("chunks")
