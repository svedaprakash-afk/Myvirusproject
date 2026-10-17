"""GST documents (GSTR-1 reported documents and the e-invoice IRP register)

Expand step: one new table.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('gst_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=6), nullable=False),
    sa.Column('doc_type', sa.String(length=3), nullable=False),
    sa.Column('doc_no', sa.String(length=40), nullable=False),
    sa.Column('doc_no_norm', sa.String(length=40), nullable=False),
    sa.Column('doc_date', sa.Date(), nullable=False),
    sa.Column('party_gstin', sa.String(length=15), nullable=True),
    sa.Column('taxable_minor', sa.BigInteger(), nullable=False),
    sa.Column('total_minor', sa.BigInteger(), nullable=False),
    sa.Column('irn', sa.String(length=64), nullable=True),
    sa.Column('ack_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=3), nullable=False),
    sa.Column('period', sa.String(length=6), nullable=True),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['legal_entities.id'], name=op.f('fk_gst_documents_entity_id_legal_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gst_documents')),
    sa.UniqueConstraint('entity_id', 'source', 'doc_type', 'doc_no_norm', name=op.f('uq_gst_documents_entity_id_source_doc_type_doc_no_norm'))
    )
    with op.batch_alter_table('gst_documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gst_documents_doc_no_norm'), ['doc_no_norm'], unique=False)
        batch_op.create_index(batch_op.f('ix_gst_documents_entity_id'), ['entity_id'], unique=False)


def downgrade() -> None:
    op.drop_table('gst_documents')
