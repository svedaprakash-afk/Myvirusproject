"""posting batch export key

Expand step (safe for rolling deploys): a nullable column recording where an exported file is stored. Old code ignores it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 21:02:29.790312
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('posting_batches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('export_key', sa.String(length=500), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('posting_batches', schema=None) as batch_op:
        batch_op.drop_column('export_key')

