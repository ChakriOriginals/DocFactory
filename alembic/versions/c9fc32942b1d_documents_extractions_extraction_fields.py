"""documents, extractions, extraction_fields

Initial pipeline schema. status is text + named CHECK constraint (not a native
enum) so later statuses are a transactional constraint swap; the downgrade is
therefore enum-safe by construction — plain drops in reverse dependency order,
no orphaned PG types.

Revision ID: c9fc32942b1d
Revises:
Create Date: 2026-08-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9fc32942b1d'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    
    op.create_table('documents',
    sa.Column('tenant_id', sa.Text(), nullable=False),
    sa.Column('s3_key', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('doc_type', sa.Text(), server_default='invoice', nullable=False),
    sa.Column('layout', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='received', nullable=False),
    sa.Column('text_s3_key', sa.Text(), nullable=True),
    sa.Column('text_chars', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('parsed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('extracted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('received', 'parsing', 'parsed', 'extracting', 'extracted', 'needs_ocr', 'failed')", name='ck_documents_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'sha256', name='uq_documents_tenant_sha256')
    )
    op.create_index('ix_documents_tenant_status', 'documents', ['tenant_id', 'status'], unique=False)
    op.create_table('extractions',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('validation', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('validation_passed', sa.Boolean(), nullable=True),
    sa.Column('doc_confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_extractions_document_created', 'extractions', ['document_id', 'created_at'], unique=False)
    op.create_table('extraction_fields',
    sa.Column('extraction_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['extraction_id'], ['extractions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('extraction_id', 'name', name='uq_extraction_fields_extraction_name')
    )
    


def downgrade() -> None:
    """Downgrade schema."""
    
    op.drop_table('extraction_fields')
    op.drop_index('ix_extractions_document_created', table_name='extractions')
    op.drop_table('extractions')
    op.drop_index('ix_documents_tenant_status', table_name='documents')
    op.drop_table('documents')
    
