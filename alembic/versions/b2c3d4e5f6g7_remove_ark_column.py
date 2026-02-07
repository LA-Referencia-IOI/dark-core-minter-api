"""Add unique constraint on naan+name

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-02-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite requires batch mode for adding constraints
    with op.batch_alter_table('ark_records', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_naan_name', ['naan', 'name'])
    
    # Add index on naan+name for performance
    op.create_index('ix_naan_name', 'ark_records', ['naan', 'name'])


def downgrade() -> None:
    # Drop the index
    op.drop_index('ix_naan_name', table_name='ark_records')
    
    # SQLite requires batch mode for removing constraints
    with op.batch_alter_table('ark_records', schema=None) as batch_op:
        batch_op.drop_constraint('uq_naan_name', type_='unique')
