"""Add original metadata media type.

Revision ID: 0002_add_original_media_type
Revises: 0001_initial_schema
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_original_media_type"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ark_metadata", sa.Column("original_media_type", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("ark_metadata", "original_media_type")
