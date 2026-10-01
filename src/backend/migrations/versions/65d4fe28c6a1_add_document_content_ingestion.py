"""store extracted document text and expose indexed job status

Revision ID: 65d4fe28c6a1
Revises: 3a8f6be59e66
Create Date: 2026-10-01 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "65d4fe28c6a1"
down_revision: str | None = "3a8f6be59e66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metadata_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "document_versions",
        sa.Column("filename", sa.String(length=512), nullable=False, server_default=""),
    )
    op.alter_column("document_versions", "filename", server_default=None)
    op.add_column(
        "document_versions", sa.Column("extracted_text", sa.Text(), nullable=True)
    )
    op.add_column(
        "document_versions",
        sa.Column(
            "extraction_metadata",
            metadata_type,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column("document_versions", "extraction_metadata", server_default=None)
    op.drop_constraint("ck_ingestion_job_status", "ingestion_jobs", type_="check")
    op.create_check_constraint(
        "ck_ingestion_job_status",
        "ingestion_jobs",
        "status IN ('pending', 'queued', 'processing', 'completed', 'indexed', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ingestion_job_status", "ingestion_jobs", type_="check")
    op.create_check_constraint(
        "ck_ingestion_job_status",
        "ingestion_jobs",
        "status IN ('pending', 'queued', 'processing', 'completed', 'failed')",
    )
    op.drop_column("document_versions", "extraction_metadata")
    op.drop_column("document_versions", "extracted_text")
    op.drop_column("document_versions", "filename")
