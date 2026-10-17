"""period locks

Expand step: one new table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 13:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('period_locks',
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('locked_through', sa.Date(), nullable=False),
    sa.Column('locked_by', sa.Integer(), nullable=True),
    sa.Column('locked_at', sa.DateTime(), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_period_locks_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['locked_by'], ['users.id'], name=op.f('fk_period_locks_locked_by_users')),
    sa.PrimaryKeyConstraint('entity_id', name=op.f('pk_period_locks'))
    )


def downgrade() -> None:
    op.drop_table('period_locks')
