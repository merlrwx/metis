"""Track external source synchronization and document identity."""

import sqlalchemy as sa
from alembic import op

revision = "a1839c7d04f2"
down_revision = "5a9f1b6d8c22"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("sync_checkpoint", sa.Text()))
    op.add_column(
        "sources",
        sa.Column("sync_status", sa.String(16), nullable=False, server_default="idle"),
    )
    op.add_column("sources", sa.Column("sync_started_at", sa.DateTime(timezone=True)))
    op.add_column("sources", sa.Column("last_synced_at", sa.DateTime(timezone=True)))
    op.add_column("sources", sa.Column("sync_error", sa.String(255)))
    op.add_column("documents", sa.Column("external_id", sa.String(512)))
    op.add_column("documents", sa.Column("external_etag", sa.String(1024)))
    op.add_column(
        "documents", sa.Column("external_modified_at", sa.DateTime(timezone=True))
    )
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint(
        "uq_document_external_source",
        "documents",
        ["organisation_id", "source_id", "external_id"],
    )


def downgrade():
    op.drop_constraint("uq_document_external_source", "documents", type_="unique")
    for name in ["deleted_at", "external_modified_at", "external_etag", "external_id"]:
        op.drop_column("documents", name)
    for name in [
        "sync_error",
        "last_synced_at",
        "sync_started_at",
        "sync_status",
        "sync_checkpoint",
    ]:
        op.drop_column("sources", name)
