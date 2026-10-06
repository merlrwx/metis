"""Allow model dimensions and track corpus maintenance explicitly."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision = "b2874d19c5e1"
down_revision = "a1839c7d04f2"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("chunks", "embedding", type_=VECTOR(), existing_type=VECTOR(1536))
    op.add_column(
        "organisations",
        sa.Column(
            "index_status", sa.String(16), nullable=False, server_default="ready"
        ),
    )
    op.add_column("organisations", sa.Column("index_model", sa.String(255)))


def downgrade():
    # Never truncate or pad another model's embeddings during a rollback.
    connection = op.get_bind()
    incompatible = connection.scalar(
        sa.text("SELECT count(*) FROM chunks WHERE vector_dims(embedding) <> 1536")
    )
    if incompatible:
        raise RuntimeError("Re-index with a 1536-dimensional model before downgrading")
    op.alter_column("chunks", "embedding", type_=VECTOR(1536), existing_type=VECTOR())
    op.drop_column("organisations", "index_model")
    op.drop_column("organisations", "index_status")
