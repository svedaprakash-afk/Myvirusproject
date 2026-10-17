"""tds entry kind (income_tax | gst)

Expand step: a defaulted column. Existing rows are income-tax TDS; combined '+GST-TDS' deductions recorded before this
release stay as one income-tax row (they cannot be split reliably after the fact).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tds_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=12), nullable=False, server_default='income_tax'))


def downgrade() -> None:
    with op.batch_alter_table('tds_entries', schema=None) as batch_op:
        batch_op.drop_column('kind')
