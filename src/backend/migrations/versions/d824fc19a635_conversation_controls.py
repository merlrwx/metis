"""Persist conversation scopes, retries and feedback."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d824fc19a635"
down_revision = "c918e274a0f3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations",
        sa.Column("scope", JSONB(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column("messages", sa.Column("outcome", sa.String(32)))
    op.create_unique_constraint(
        "uq_message_id_organisation", "messages", ["id", "organisation_id"]
    )
    op.create_table(
        "chat_request_records",
        sa.Column("organisation_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organisation_id"],
            ["conversations.id", "conversations.organisation_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "message_feedback",
        sa.Column("message_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("vote", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32)),
        sa.Column("comment", sa.String(1000)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "organisation_id"],
            ["messages.id", "messages.organisation_id"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("message_feedback")
    op.drop_table("chat_request_records")
    op.drop_constraint("uq_message_id_organisation", "messages", type_="unique")
    op.drop_column("messages", "outcome")
    op.drop_column("conversations", "scope")
