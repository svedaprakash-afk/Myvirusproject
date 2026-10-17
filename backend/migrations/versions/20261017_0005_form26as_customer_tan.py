"""Form 26AS / AIS entries and customer TAN

Expand step: one nullable column and one new table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17 11:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tan', sa.String(length=10), nullable=True))
        batch_op.create_index(batch_op.f('ix_customers_tan'), ['tan'], unique=False)
    op.create_table('form26as_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=6), nullable=False),
    sa.Column('fy', sa.String(length=9), nullable=False),
    sa.Column('quarter', sa.String(length=2), nullable=False),
    sa.Column('deductor_tan', sa.String(length=10), nullable=False),
    sa.Column('deductor_name', sa.String(length=250), nullable=False),
    sa.Column('section', sa.String(length=20), nullable=True),
    sa.Column('txn_date', sa.Date(), nullable=False),
    sa.Column('booking_date', sa.Date(), nullable=True),
    sa.Column('booking_status', sa.String(length=2), nullable=True),
    sa.Column('amount_paid_minor', sa.BigInteger(), nullable=False),
    sa.Column('tax_deducted_minor', sa.BigInteger(), nullable=False),
    sa.Column('tds_deposited_minor', sa.BigInteger(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_form26as_entries_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_form26as_entries_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_form26as_entries')),
    sa.UniqueConstraint('entity_id', 'source', 'dedupe_key', name=op.f('uq_form26as_entries_entity_id_source_dedupe_key'))
    )
    with op.batch_alter_table('form26as_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_form26as_entries_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_form26as_entries_deductor_tan'), ['deductor_tan'], unique=False)
        batch_op.create_index(batch_op.f('ix_form26as_entries_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_form26as_entries_fy'), ['fy'], unique=False)


def downgrade() -> None:
    op.drop_table('form26as_entries')
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customers_tan'))
        batch_op.drop_column('tan')
