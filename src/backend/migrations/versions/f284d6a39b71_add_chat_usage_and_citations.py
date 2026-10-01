"""store citations and model usage on chat messages

Revision ID: f284d6a39b71
Revises: d42f6ed4ca17
Create Date: 2026-10-01 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f284d6a39b71"
down_revision: str | None = "d42f6ed4ca17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    citations_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("messages", sa.Column("message_index", sa.Integer()))
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            WITH ordered AS (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY organisation_id, conversation_id
                    ORDER BY created_at, id
                ) - 1 AS message_index
                FROM messages
            )
            UPDATE messages
            SET message_index = ordered.message_index
            FROM ordered
            WHERE messages.id = ordered.id
            """
        )
    )
    op.alter_column(
        "messages", "message_index", existing_type=sa.Integer(), nullable=False
    )
    op.add_column(
        "messages",
        sa.Column(
            "citations",
            citations_type,
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column("messages", sa.Column("model_id", sa.String(length=255)))
    op.add_column("messages", sa.Column("input_tokens", sa.Integer()))
    op.add_column("messages", sa.Column("output_tokens", sa.Integer()))
    op.create_unique_constraint(
        "uq_message_conversation_index",
        "messages",
        ["conversation_id", "message_index"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_message_conversation_index", "messages", type_="unique")
    op.drop_column("messages", "output_tokens")
    op.drop_column("messages", "input_tokens")
    op.drop_column("messages", "model_id")
    op.drop_column("messages", "citations")
    op.drop_column("messages", "message_index")
