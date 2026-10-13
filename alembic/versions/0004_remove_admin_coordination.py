"""Remove abandoned cross-service ownership coordination; retain old audit data.

0003 is kept immutable for databases that already applied it. Legacy transfer
rows survive as historical audit, but neither Admin nor Minter uses them.
"""
from alembic import op
import sqlalchemy as sa
revision = '0004_remove_admin_coordination'
down_revision = '0003_authority_retirement'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('authority_retirements')
    op.rename_table('ark_owner_transfers', 'legacy_ark_owner_transfers')
    op.create_table('superseded_ark_jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ark', sa.String(255), nullable=False),
        sa.Column('original_authority_id', sa.String(255), nullable=False),
        sa.Column('replacement_authority_id', sa.String(255), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()))


def downgrade():
    op.drop_table('superseded_ark_jobs')
    op.rename_table('legacy_ark_owner_transfers', 'ark_owner_transfers')
    op.create_table('authority_retirements',
        sa.Column('authority_id', sa.String(255), primary_key=True),
        sa.Column('retired', sa.Boolean(), nullable=False, server_default=sa.false()))
