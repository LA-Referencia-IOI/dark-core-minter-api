"""Persistent replay protection for signed requests."""
from alembic import op
import sqlalchemy as sa
revision = "0002_authority_request_nonces"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("authority_request_nonces",
        sa.Column("authority_id", sa.String(255), primary_key=True),
        sa.Column("nonce", sa.String(36), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_authority_request_nonces_expires_at", "authority_request_nonces", ["expires_at"])


def downgrade():
    op.drop_table("authority_request_nonces")
