"""Replace metadata_json with metadata_format

Revision ID: a1b2c3d4e5f6
Revises: d8bae116f993
Create Date: 2026-02-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'd8bae116f993'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace metadata_json column with metadata_format."""
    # Add new metadata_format column
    op.add_column(
        'ark_records',
        sa.Column('metadata_format', sa.String(20), nullable=True)
    )
    
    # Drop the old metadata_json column
    op.drop_column('ark_records', 'metadata_json')


def downgrade() -> None:
    """Restore metadata_json column, remove metadata_format."""
    # Add back metadata_json column
    op.add_column(
        'ark_records',
        sa.Column('metadata_json', sa.JSON(), nullable=True)
    )
    
    # Drop metadata_format column
    op.drop_column('ark_records', 'metadata_format')
