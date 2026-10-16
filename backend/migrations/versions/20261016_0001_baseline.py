"""baseline

Revision ID: 0001
Revises: 
Create Date: 2026-10-16 20:45:36.889420
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


# --- PostgreSQL enum handling -----------------------------------------------------------------------------------------------
# Several tables share an enum type (e.g. 'role'). Alembic's create_table would emit CREATE TYPE once per column and fail on
# PostgreSQL, so types are created ONCE up front (checkfirst) and referenced with create_type=False. On other dialects the same
# type degrades to VARCHAR, exactly as the models do.
ENUMS = {
    'approvalkind': ('write_off', 'adjustment', 'manual_match', 'batch_release', 'refund'),
    'approvalstatus': ('pending', 'approved', 'rejected'),
    'batchstatus': ('draft', 'approved', 'exported', 'acknowledged', 'failed'),
    'exceptionstatus': ('open', 'in_progress', 'pending_approval', 'resolved', 'written_off'),
    'exceptiontype': ('unidentified_receipt', 'short_pay_tds', 'short_pay', 'overpayment', 'on_account', 'duplicate_payment', 'intercompany', 'non_ar_credit', 'disputed_invoice', 'deduction'),
    'proposalstatus': ('pending_review', 'auto_approved', 'accepted', 'rejected', 'superseded', 'reversed', 'posted'),
    'role': ('admin', 'ar_analyst', 'ar_manager', 'treasury', 'finance_head', 'auditor'),
    'txnstatus': ('new', 'review', 'matched', 'partially_matched', 'exception', 'posted', 'ignored'),
}


def _E(*values, name):
    return postgresql.ENUM(*values, name=name, create_type=False)


def _create_enums() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for n, v in ENUMS.items():
            postgresql.ENUM(*v, name=n).create(bind, checkfirst=True)


def _drop_enums() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for n, v in ENUMS.items():
            postgresql.ENUM(*v, name=n).drop(bind, checkfirst=True)


def upgrade() -> None:
    _create_enums()
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('actor', sa.String(length=120), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('object_type', sa.String(length=40), nullable=False),
    sa.Column('object_id', sa.String(length=40), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log')),
    sa.UniqueConstraint('seq', name=op.f('uq_audit_log_seq'))
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_log_object_id'), ['object_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_log_object_type'), ['object_type'], unique=False)

    op.create_table('exchange_rates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('rate_date', sa.Date(), nullable=False),
    sa.Column('rate_micro', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exchange_rates')),
    sa.UniqueConstraint('currency', 'rate_date', name=op.f('uq_exchange_rates_currency_rate_date'))
    )
    with op.batch_alter_table('exchange_rates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exchange_rates_currency'), ['currency'], unique=False)
        batch_op.create_index(batch_op.f('ix_exchange_rates_rate_date'), ['rate_date'], unique=False)

    op.create_table('job_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('slot', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_runs')),
    sa.UniqueConstraint('name', 'slot', name=op.f('uq_job_runs_name_slot'))
    )
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_job_runs_name'), ['name'], unique=False)

    op.create_table('legal_entities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('gstin', sa.String(length=15), nullable=True),
    sa.Column('pan', sa.String(length=10), nullable=True),
    sa.Column('base_currency', sa.String(length=3), nullable=False),
    sa.Column('config', sa.JSON(), nullable=False),
    sa.Column('ledgers', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_legal_entities'))
    )
    op.create_table('scoring_models',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('version', sa.String(length=20), nullable=False),
    sa.Column('weights', sa.JSON(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scoring_models')),
    sa.UniqueConstraint('version', name=op.f('uq_scoring_models_version'))
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('role', _E('admin', 'ar_analyst', 'ar_manager', 'treasury', 'finance_head', 'auditor', name='role'), nullable=False),
    sa.Column('password_hash', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('entity_ids', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('approval_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('kind', _E('write_off', 'adjustment', 'manual_match', 'batch_release', 'refund', name='approvalkind'), nullable=False),
    sa.Column('min_minor', sa.BigInteger(), nullable=False),
    sa.Column('max_minor', sa.BigInteger(), nullable=True),
    sa.Column('approver_role', _E('admin', 'ar_analyst', 'ar_manager', 'treasury', 'finance_head', 'auditor', name='role'), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_approval_policies_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approval_policies'))
    )
    with op.batch_alter_table('approval_policies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_approval_policies_entity_id'), ['entity_id'], unique=False)

    op.create_table('approval_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('kind', _E('write_off', 'adjustment', 'manual_match', 'batch_release', 'refund', name='approvalkind'), nullable=False),
    sa.Column('status', _E('pending', 'approved', 'rejected', name='approvalstatus'), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('required_role', _E('admin', 'ar_analyst', 'ar_manager', 'treasury', 'finance_head', 'auditor', name='role'), nullable=False),
    sa.Column('requested_by', sa.Integer(), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('ref_type', sa.String(length=30), nullable=True),
    sa.Column('ref_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_approval_requests_decided_by_users')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_approval_requests_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_approval_requests_requested_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approval_requests'))
    )
    with op.batch_alter_table('approval_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_approval_requests_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_approval_requests_status'), ['status'], unique=False)

    op.create_table('bank_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('bank_name', sa.String(length=100), nullable=False),
    sa.Column('account_no', sa.String(length=34), nullable=False),
    sa.Column('ifsc', sa.String(length=11), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('ledger_code', sa.String(length=50), nullable=False),
    sa.Column('van_prefix', sa.String(length=20), nullable=True),
    sa.Column('is_gateway', sa.Boolean(), nullable=False),
    sa.Column('profile', sa.JSON(), nullable=False),
    sa.Column('feed', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_bank_accounts_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bank_accounts')),
    sa.UniqueConstraint('entity_id', 'account_no', name=op.f('uq_bank_accounts_entity_id_account_no'))
    )
    op.create_table('customers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=250), nullable=False),
    sa.Column('gstin', sa.String(length=15), nullable=True),
    sa.Column('pan', sa.String(length=10), nullable=True),
    sa.Column('payment_terms_days', sa.Integer(), nullable=False),
    sa.Column('tier', sa.String(length=10), nullable=False),
    sa.Column('tds_rate_bp', sa.Integer(), nullable=True),
    sa.Column('tds_section', sa.String(length=20), nullable=True),
    sa.Column('tds_base', sa.String(length=10), nullable=False),
    sa.Column('virtual_account_no', sa.String(length=34), nullable=True),
    sa.Column('interco_entity_id', sa.Integer(), nullable=True),
    sa.Column('ar_ledger', sa.String(length=50), nullable=True),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_customers_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['interco_entity_id'], ['legal_entities.id'], name=op.f('fk_customers_interco_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers')),
    sa.UniqueConstraint('entity_id', 'code', name=op.f('uq_customers_entity_id_code'))
    )
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customers_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customers_gstin'), ['gstin'], unique=False)
        batch_op.create_index(batch_op.f('ix_customers_pan'), ['pan'], unique=False)
        batch_op.create_index(batch_op.f('ix_customers_virtual_account_no'), ['virtual_account_no'], unique=False)

    op.create_table('posting_batches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('status', _E('draft', 'approved', 'exported', 'acknowledged', 'failed', name='batchstatus'), nullable=False),
    sa.Column('connector', sa.String(length=40), nullable=True),
    sa.Column('idempotency_key', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('approved_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('exported_at', sa.DateTime(), nullable=True),
    sa.Column('acked_at', sa.DateTime(), nullable=True),
    sa.Column('checksum', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('total_minor', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_posting_batches_approved_by_users')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_posting_batches_created_by_users')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_posting_batches_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_posting_batches')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_posting_batches_idempotency_key'))
    )
    with op.batch_alter_table('posting_batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_posting_batches_entity_id'), ['entity_id'], unique=False)

    op.create_table('customer_identifiers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('value', sa.String(length=200), nullable=False),
    sa.Column('hits', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('last_seen', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_customer_identifiers_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_customer_identifiers_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customer_identifiers')),
    sa.UniqueConstraint('entity_id', 'customer_id', 'kind', 'value', name=op.f('uq_customer_identifiers_entity_id_customer_id_kind_value'))
    )
    with op.batch_alter_table('customer_identifiers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_identifiers_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_identifiers_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_identifiers_value'), ['value'], unique=False)

    op.create_table('customer_profiles',
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('payments', sa.Integer(), nullable=False),
    sa.Column('multi_invoice_payments', sa.Integer(), nullable=False),
    sa.Column('tds_payments', sa.Integer(), nullable=False),
    sa.Column('ref_in_narration', sa.Integer(), nullable=False),
    sa.Column('avg_days_to_pay', sa.Float(), nullable=False),
    sa.Column('avg_days_late', sa.Float(), nullable=False),
    sa.Column('last_paid', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_customer_profiles_customer_id_customers')),
    sa.PrimaryKeyConstraint('customer_id', name=op.f('pk_customer_profiles'))
    )
    op.create_table('invoices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=False),
    sa.Column('doc_type', sa.String(length=12), nullable=False),
    sa.Column('invoice_date', sa.Date(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('taxable_minor', sa.BigInteger(), nullable=False),
    sa.Column('gst_minor', sa.BigInteger(), nullable=False),
    sa.Column('total_minor', sa.BigInteger(), nullable=False),
    sa.Column('open_minor', sa.BigInteger(), nullable=False),
    sa.Column('irn', sa.String(length=64), nullable=True),
    sa.Column('ack_no', sa.String(length=30), nullable=True),
    sa.Column('eway_bill_no', sa.String(length=20), nullable=True),
    sa.Column('po_ref', sa.String(length=60), nullable=True),
    sa.Column('disputed', sa.Boolean(), nullable=False),
    sa.Column('erp_ref', sa.String(length=80), nullable=True),
    sa.Column('booking_rate_micro', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_invoices_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_invoices_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invoices')),
    sa.UniqueConstraint('entity_id', 'number', name=op.f('uq_invoices_entity_id_number'))
    )
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.create_index('ix_inv_open', ['entity_id', 'customer_id', 'open_minor'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoices_irn'), ['irn'], unique=False)

    op.create_table('remittance_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('filename', sa.String(length=250), nullable=True),
    sa.Column('raw_text', sa.Text(), nullable=False),
    sa.Column('extracted', sa.JSON(), nullable=False),
    sa.Column('extractor', sa.String(length=30), nullable=False),
    sa.Column('source_ref', sa.String(length=200), nullable=True),
    sa.Column('sender', sa.String(length=250), nullable=True),
    sa.Column('auth_result', sa.String(length=60), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('linked_txn_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_remittance_documents_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_remittance_documents_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_remittance_documents'))
    )
    with op.batch_alter_table('remittance_documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_remittance_documents_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_remittance_documents_source_ref'), ['source_ref'], unique=False)

    op.create_table('tolerance_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('abs_minor', sa.BigInteger(), nullable=False),
    sa.Column('pct_bp', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_tolerance_rules_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_tolerance_rules_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tolerance_rules'))
    )
    with op.batch_alter_table('tolerance_rules', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tolerance_rules_entity_id'), ['entity_id'], unique=False)

    op.create_table('payment_transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('bank_account_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=30), nullable=False),
    sa.Column('source_batch', sa.String(length=60), nullable=True),
    sa.Column('txn_date', sa.Date(), nullable=False),
    sa.Column('value_date', sa.Date(), nullable=True),
    sa.Column('direction', sa.String(length=6), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('fee_minor', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('channel', sa.String(length=12), nullable=False),
    sa.Column('utr', sa.String(length=40), nullable=True),
    sa.Column('narration', sa.Text(), nullable=False),
    sa.Column('payer_name', sa.String(length=250), nullable=True),
    sa.Column('payer_account', sa.String(length=34), nullable=True),
    sa.Column('payer_ifsc', sa.String(length=11), nullable=True),
    sa.Column('payer_vpa', sa.String(length=100), nullable=True),
    sa.Column('bank_ref', sa.String(length=60), nullable=True),
    sa.Column('balance_minor', sa.BigInteger(), nullable=True),
    sa.Column('extra', sa.JSON(), nullable=False),
    sa.Column('fx_ccy', sa.String(length=3), nullable=True),
    sa.Column('fx_minor', sa.BigInteger(), nullable=True),
    sa.Column('fx_rate_micro', sa.BigInteger(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('status', _E('new', 'review', 'matched', 'partially_matched', 'exception', 'posted', 'ignored', name='txnstatus'), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('customer_confidence', sa.Float(), nullable=True),
    sa.Column('remittance_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('matched_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['bank_account_id'], ['bank_accounts.id'], name=op.f('fk_payment_transactions_bank_account_id_bank_accounts')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_payment_transactions_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_payment_transactions_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['remittance_id'], ['remittance_documents.id'], name=op.f('fk_payment_transactions_remittance_id_remittance_documents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_transactions')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_payment_transactions_dedupe_key'))
    )
    with op.batch_alter_table('payment_transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payment_transactions_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_transactions_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_transactions_txn_date'), ['txn_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_transactions_utr'), ['utr'], unique=False)

    op.create_table('match_proposals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('txn_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('status', _E('pending_review', 'auto_approved', 'accepted', 'rejected', 'superseded', 'reversed', 'posted', name='proposalstatus'), nullable=False),
    sa.Column('strategy', sa.String(length=40), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('model_version', sa.String(length=20), nullable=False),
    sa.Column('explanation', sa.JSON(), nullable=False),
    sa.Column('features', sa.JSON(), nullable=False),
    sa.Column('guardrails', sa.JSON(), nullable=False),
    sa.Column('applied_minor', sa.BigInteger(), nullable=False),
    sa.Column('tds_minor', sa.BigInteger(), nullable=False),
    sa.Column('writeoff_minor', sa.BigInteger(), nullable=False),
    sa.Column('fee_minor', sa.BigInteger(), nullable=False),
    sa.Column('unapplied_minor', sa.BigInteger(), nullable=False),
    sa.Column('short_minor', sa.BigInteger(), nullable=False),
    sa.Column('settle_currency', sa.String(length=3), nullable=True),
    sa.Column('settle_rate_micro', sa.BigInteger(), nullable=True),
    sa.Column('writeoff_kind', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=60), nullable=False),
    sa.Column('auto', sa.Boolean(), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['posting_batches.id'], name=op.f('fk_match_proposals_batch_id_posting_batches')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_match_proposals_customer_id_customers')),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_match_proposals_decided_by_users')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_match_proposals_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['txn_id'], ['payment_transactions.id'], name=op.f('fk_match_proposals_txn_id_payment_transactions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_match_proposals'))
    )
    with op.batch_alter_table('match_proposals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_match_proposals_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_match_proposals_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_match_proposals_txn_id'), ['txn_id'], unique=False)

    op.create_table('exception_cases',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('type', _E('unidentified_receipt', 'short_pay_tds', 'short_pay', 'overpayment', 'on_account', 'duplicate_payment', 'intercompany', 'non_ar_credit', 'disputed_invoice', 'deduction', name='exceptiontype'), nullable=False),
    sa.Column('status', _E('open', 'in_progress', 'pending_approval', 'resolved', 'written_off', name='exceptionstatus'), nullable=False),
    sa.Column('txn_id', sa.Integer(), nullable=True),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('proposal_id', sa.Integer(), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('suggestions', sa.JSON(), nullable=False),
    sa.Column('priority', sa.Float(), nullable=False),
    sa.Column('assignee_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('resolution', sa.String(length=60), nullable=True),
    sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], name=op.f('fk_exception_cases_assignee_id_users')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_exception_cases_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_exception_cases_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_exception_cases_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['proposal_id'], ['match_proposals.id'], name=op.f('fk_exception_cases_proposal_id_match_proposals')),
    sa.ForeignKeyConstraint(['txn_id'], ['payment_transactions.id'], name=op.f('fk_exception_cases_txn_id_payment_transactions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exception_cases'))
    )
    with op.batch_alter_table('exception_cases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exception_cases_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exception_cases_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_exception_cases_txn_id'), ['txn_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exception_cases_type'), ['type'], unique=False)

    op.create_table('match_allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('applied_minor', sa.BigInteger(), nullable=False),
    sa.Column('tds_minor', sa.BigInteger(), nullable=False),
    sa.Column('writeoff_minor', sa.BigInteger(), nullable=False),
    sa.Column('tds_section', sa.String(length=20), nullable=True),
    sa.Column('tds_rate_bp', sa.Integer(), nullable=True),
    sa.Column('reference_hit', sa.String(length=20), nullable=True),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_match_allocations_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['proposal_id'], ['match_proposals.id'], name=op.f('fk_match_allocations_proposal_id_match_proposals')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_match_allocations'))
    )
    with op.batch_alter_table('match_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_match_allocations_invoice_id'), ['invoice_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_match_allocations_proposal_id'), ['proposal_id'], unique=False)

    op.create_table('match_feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=True),
    sa.Column('label', sa.Integer(), nullable=False),
    sa.Column('features', sa.JSON(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_match_feedback_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['proposal_id'], ['match_proposals.id'], name=op.f('fk_match_feedback_proposal_id_match_proposals')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_match_feedback_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_match_feedback'))
    )
    with op.batch_alter_table('match_feedback', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_match_feedback_entity_id'), ['entity_id'], unique=False)

    op.create_table('tds_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.Column('txn_date', sa.Date(), nullable=False),
    sa.Column('fy', sa.String(length=9), nullable=False),
    sa.Column('quarter', sa.String(length=2), nullable=False),
    sa.Column('section', sa.String(length=20), nullable=True),
    sa.Column('rate_bp', sa.Integer(), nullable=True),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('certificate_received', sa.Boolean(), nullable=False),
    sa.Column('in_26as', sa.Boolean(), nullable=False),
    sa.Column('reversed', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_tds_entries_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_tds_entries_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_tds_entries_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['proposal_id'], ['match_proposals.id'], name=op.f('fk_tds_entries_proposal_id_match_proposals')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tds_entries'))
    )
    with op.batch_alter_table('tds_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tds_entries_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tds_entries_entity_id'), ['entity_id'], unique=False)

    op.create_table('adjustments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('txn_id', sa.Integer(), nullable=True),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('exception_id', sa.Integer(), nullable=True),
    sa.Column('approval_id', sa.Integer(), nullable=True),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('debit_account', sa.String(length=60), nullable=False),
    sa.Column('credit_account', sa.String(length=60), nullable=False),
    sa.Column('reason', sa.String(length=300), nullable=False),
    sa.Column('posting_date', sa.Date(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('posted', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['approval_requests.id'], name=op.f('fk_adjustments_approval_id_approval_requests')),
    sa.ForeignKeyConstraint(['batch_id'], ['posting_batches.id'], name=op.f('fk_adjustments_batch_id_posting_batches')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_adjustments_created_by_users')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_adjustments_customer_id_customers')),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_adjustments_entity_id_legal_entities')),
    sa.ForeignKeyConstraint(['exception_id'], ['exception_cases.id'], name=op.f('fk_adjustments_exception_id_exception_cases')),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_adjustments_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['txn_id'], ['payment_transactions.id'], name=op.f('fk_adjustments_txn_id_payment_transactions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_adjustments'))
    )
    with op.batch_alter_table('adjustments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_adjustments_entity_id'), ['entity_id'], unique=False)

    op.create_table('exception_notes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['exception_cases.id'], name=op.f('fk_exception_notes_case_id_exception_cases')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_exception_notes_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exception_notes'))
    )
    with op.batch_alter_table('exception_notes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exception_notes_case_id'), ['case_id'], unique=False)

    op.create_table('posting_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=False),
    sa.Column('voucher_no', sa.String(length=40), nullable=False),
    sa.Column('txn_id', sa.Integer(), nullable=True),
    sa.Column('proposal_id', sa.Integer(), nullable=True),
    sa.Column('adjustment_id', sa.Integer(), nullable=True),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('line_type', sa.String(length=24), nullable=False),
    sa.Column('account', sa.String(length=60), nullable=False),
    sa.Column('debit_minor', sa.BigInteger(), nullable=False),
    sa.Column('credit_minor', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('base_debit_minor', sa.BigInteger(), nullable=False),
    sa.Column('base_credit_minor', sa.BigInteger(), nullable=False),
    sa.Column('rate_micro', sa.BigInteger(), nullable=True),
    sa.Column('posting_date', sa.Date(), nullable=False),
    sa.Column('narration', sa.String(length=300), nullable=False),
    sa.Column('erp_doc_no', sa.String(length=80), nullable=True),
    sa.ForeignKeyConstraint(['adjustment_id'], ['adjustments.id'], name=op.f('fk_posting_lines_adjustment_id_adjustments')),
    sa.ForeignKeyConstraint(['batch_id'], ['posting_batches.id'], name=op.f('fk_posting_lines_batch_id_posting_batches')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_posting_lines_customer_id_customers')),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_posting_lines_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['proposal_id'], ['match_proposals.id'], name=op.f('fk_posting_lines_proposal_id_match_proposals')),
    sa.ForeignKeyConstraint(['txn_id'], ['payment_transactions.id'], name=op.f('fk_posting_lines_txn_id_payment_transactions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_posting_lines'))
    )
    with op.batch_alter_table('posting_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_posting_lines_batch_id'), ['batch_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_posting_lines_voucher_no'), ['voucher_no'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('posting_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_posting_lines_voucher_no'))
        batch_op.drop_index(batch_op.f('ix_posting_lines_batch_id'))

    op.drop_table('posting_lines')
    with op.batch_alter_table('exception_notes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exception_notes_case_id'))

    op.drop_table('exception_notes')
    with op.batch_alter_table('adjustments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_adjustments_entity_id'))

    op.drop_table('adjustments')
    with op.batch_alter_table('tds_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tds_entries_entity_id'))
        batch_op.drop_index(batch_op.f('ix_tds_entries_customer_id'))

    op.drop_table('tds_entries')
    with op.batch_alter_table('match_feedback', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_match_feedback_entity_id'))

    op.drop_table('match_feedback')
    with op.batch_alter_table('match_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_match_allocations_proposal_id'))
        batch_op.drop_index(batch_op.f('ix_match_allocations_invoice_id'))

    op.drop_table('match_allocations')
    with op.batch_alter_table('exception_cases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exception_cases_type'))
        batch_op.drop_index(batch_op.f('ix_exception_cases_txn_id'))
        batch_op.drop_index(batch_op.f('ix_exception_cases_status'))
        batch_op.drop_index(batch_op.f('ix_exception_cases_entity_id'))

    op.drop_table('exception_cases')
    with op.batch_alter_table('match_proposals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_match_proposals_txn_id'))
        batch_op.drop_index(batch_op.f('ix_match_proposals_status'))
        batch_op.drop_index(batch_op.f('ix_match_proposals_entity_id'))

    op.drop_table('match_proposals')
    with op.batch_alter_table('payment_transactions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payment_transactions_utr'))
        batch_op.drop_index(batch_op.f('ix_payment_transactions_txn_date'))
        batch_op.drop_index(batch_op.f('ix_payment_transactions_status'))
        batch_op.drop_index(batch_op.f('ix_payment_transactions_entity_id'))

    op.drop_table('payment_transactions')
    with op.batch_alter_table('tolerance_rules', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tolerance_rules_entity_id'))

    op.drop_table('tolerance_rules')
    with op.batch_alter_table('remittance_documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_remittance_documents_source_ref'))
        batch_op.drop_index(batch_op.f('ix_remittance_documents_entity_id'))

    op.drop_table('remittance_documents')
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invoices_irn'))
        batch_op.drop_index(batch_op.f('ix_invoices_entity_id'))
        batch_op.drop_index(batch_op.f('ix_invoices_customer_id'))
        batch_op.drop_index('ix_inv_open')

    op.drop_table('invoices')
    op.drop_table('customer_profiles')
    with op.batch_alter_table('customer_identifiers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_identifiers_value'))
        batch_op.drop_index(batch_op.f('ix_customer_identifiers_entity_id'))
        batch_op.drop_index(batch_op.f('ix_customer_identifiers_customer_id'))

    op.drop_table('customer_identifiers')
    with op.batch_alter_table('posting_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_posting_batches_entity_id'))

    op.drop_table('posting_batches')
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customers_virtual_account_no'))
        batch_op.drop_index(batch_op.f('ix_customers_pan'))
        batch_op.drop_index(batch_op.f('ix_customers_gstin'))
        batch_op.drop_index(batch_op.f('ix_customers_entity_id'))

    op.drop_table('customers')
    op.drop_table('bank_accounts')
    with op.batch_alter_table('approval_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_approval_requests_status'))
        batch_op.drop_index(batch_op.f('ix_approval_requests_entity_id'))

    op.drop_table('approval_requests')
    with op.batch_alter_table('approval_policies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_approval_policies_entity_id'))

    op.drop_table('approval_policies')
    op.drop_table('users')
    op.drop_table('scoring_models')
    op.drop_table('legal_entities')
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_job_runs_name'))

    op.drop_table('job_runs')
    with op.batch_alter_table('exchange_rates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exchange_rates_rate_date'))
        batch_op.drop_index(batch_op.f('ix_exchange_rates_currency'))

    op.drop_table('exchange_rates')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_object_type'))
        batch_op.drop_index(batch_op.f('ix_audit_log_object_id'))
        batch_op.drop_index(batch_op.f('ix_audit_log_action'))

    op.drop_table('audit_log')
    _drop_enums()
