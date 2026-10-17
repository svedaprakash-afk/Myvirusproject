"""notification outbox

Expand step: one new table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=40), nullable=False),
    sa.Column('channel', sa.String(length=10), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_notifications_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('entity_id', 'channel', 'dedupe_key', name=op.f('uq_notifications_entity_id_channel_dedupe_key'))
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_status'), ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('notifications')
