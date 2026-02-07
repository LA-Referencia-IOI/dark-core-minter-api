"""Add noid_counters table for deterministic minting.

Revision ID: 0002_add_noid_counters
Revises: 0001_consolidated_schema
Create Date: 2026-02-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_add_noid_counters"
down_revision: Union[str, None] = "0001_consolidated_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "noid_counters",
        sa.Column("namespace_key", sa.String(length=160), nullable=False),
        sa.Column("next_value", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("namespace_key"),
    )


def downgrade() -> None:
    op.drop_table("noid_counters")
