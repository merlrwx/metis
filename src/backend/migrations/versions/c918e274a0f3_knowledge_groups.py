"""Organisation-scoped saved source groups."""

import sqlalchemy as sa
from alembic import op

revision = "c918e274a0f3"
down_revision = "b2874d19c5e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organisation_id",
            sa.Uuid(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("id", "organisation_id"),
    )
    op.create_index(
        "ix_knowledge_groups_organisation_id", "knowledge_groups", ["organisation_id"]
    )
    op.create_table(
        "knowledge_group_sources",
        sa.Column("group_id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id", "organisation_id"],
            ["knowledge_groups.id", "knowledge_groups.organisation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "organisation_id"],
            ["sources.id", "sources.organisation_id"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("knowledge_group_sources")
    op.drop_index("ix_knowledge_groups_organisation_id", "knowledge_groups")
    op.drop_table("knowledge_groups")
