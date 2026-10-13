"""Administrative ownership transfer audit and authority retirement gate."""
from alembic import op
import sqlalchemy as sa
revision = '0003_authority_retirement'
down_revision = '0002_authority_request_nonces'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('authority_retirements',
        sa.Column('authority_id', sa.String(255), primary_key=True),
        sa.Column('retired', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('ark_owner_transfers',
        sa.Column('request_id', sa.String(36), primary_key=True),
        sa.Column('naan', sa.String(50), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('source_authority', sa.String(255), nullable=False),
        sa.Column('target_authority', sa.String(255), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('transaction_hash', sa.String(66)),
        sa.Column('signed_transaction', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_index('ix_ark_owner_transfers_ark', 'ark_owner_transfers', ['naan', 'name', 'status'])
    op.create_index('ix_ark_owner_transfers_source', 'ark_owner_transfers', ['source_authority', 'status'])


def downgrade():
    op.drop_table('ark_owner_transfers')
    op.drop_table('authority_retirements')
