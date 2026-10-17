"""per-customer trust, on-account balances, advance applications, shadow scoring model

Expand step: new tables plus nullable/defaulted columns only, so old code keeps working during a rolling deploy.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 08:19:38.257784
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('customer_trust',
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=14), nullable=False),
    sa.Column('clean_streak', sa.Integer(), nullable=False),
    sa.Column('confirmed', sa.Integer(), nullable=False),
    sa.Column('corrections', sa.Integer(), nullable=False),
    sa.Column('threshold_override', sa.Float(), nullable=True),
    sa.Column('pinned', sa.Boolean(), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_customer_trust_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_customer_trust_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_customer_trust_updated_by_users')),
    sa.PrimaryKeyConstraint('customer_id', name=op.f('pk_customer_trust'))
    )
    with op.batch_alter_table('customer_trust', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_trust_entity_id'), ['entity_id'], unique=False)

    op.create_table('on_account_balances',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('txn_id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('original_minor', sa.BigInteger(), nullable=False),
    sa.Column('remaining_minor', sa.BigInteger(), nullable=False),
    sa.Column('reference', sa.String(length=60), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_on_account_balances_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_on_account_balances_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['proposal_id'], ['match_proposals.id'], name=op.f('fk_on_account_balances_proposal_id_match_proposals')),
    sa.ForeignKeyConstraint(['txn_id'], ['payment_transactions.id'], name=op.f('fk_on_account_balances_txn_id_payment_transactions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_on_account_balances')),
    sa.UniqueConstraint('proposal_id', name=op.f('uq_on_account_balances_proposal_id'))
    )
    with op.batch_alter_table('on_account_balances', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_on_account_balances_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_on_account_balances_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_on_account_balances_txn_id'), ['txn_id'], unique=False)

    op.create_table('advance_applications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('balance_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('basis', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('adjustment_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['adjustment_id'], ['adjustments.id'], name=op.f('fk_advance_applications_adjustment_id_adjustments')),
    sa.ForeignKeyConstraint(['balance_id'], ['on_account_balances.id'], name=op.f('fk_advance_applications_balance_id_on_account_balances')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_advance_applications_customer_id_customers')),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_advance_applications_decided_by_users')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_advance_applications_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_advance_applications_invoice_id_invoices')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_advance_applications')),
    sa.UniqueConstraint('balance_id', 'invoice_id', name=op.f('uq_advance_applications_balance_id_invoice_id'))
    )
    with op.batch_alter_table('advance_applications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_advance_applications_balance_id'), ['balance_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_advance_applications_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_advance_applications_invoice_id'), ['invoice_id'], unique=False)

    with op.batch_alter_table('match_proposals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shadow_confidence', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('shadow_model_version', sa.String(length=20), nullable=True))

    with op.batch_alter_table('scoring_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shadow', sa.Boolean(), nullable=False, server_default=sa.false()))



def downgrade() -> None:
    with op.batch_alter_table('scoring_models', schema=None) as batch_op:
        batch_op.drop_column('shadow')

    with op.batch_alter_table('match_proposals', schema=None) as batch_op:
        batch_op.drop_column('shadow_model_version')
        batch_op.drop_column('shadow_confidence')

    with op.batch_alter_table('advance_applications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_advance_applications_invoice_id'))
        batch_op.drop_index(batch_op.f('ix_advance_applications_entity_id'))
        batch_op.drop_index(batch_op.f('ix_advance_applications_balance_id'))

    op.drop_table('advance_applications')
    with op.batch_alter_table('on_account_balances', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_on_account_balances_txn_id'))
        batch_op.drop_index(batch_op.f('ix_on_account_balances_entity_id'))
        batch_op.drop_index(batch_op.f('ix_on_account_balances_customer_id'))

    op.drop_table('on_account_balances')
    with op.batch_alter_table('customer_trust', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_trust_entity_id'))

    op.drop_table('customer_trust')
