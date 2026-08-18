"""routing: needs_review status and model provenance

Adds the two post-extraction routing outcomes to the status CHECK constraint,
plus provenance columns recording which confidence model made each call.

Widening the constraint is a transactional drop/recreate — the payoff of
choosing text + CHECK over a native PG enum back in the first migration, where
ALTER TYPE ... ADD VALUE cannot run inside a transaction and values can never
be removed.

Alembic does not diff CHECK constraints, so that half is hand-written.

Revision ID: 7efe6f688a88
Revises: 0b38442255b5
Create Date: 2026-08-17 21:56:29.431452
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7efe6f688a88"
down_revision: str | Sequence[str] | None = "0b38442255b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = (
    "received", "parsing", "parsed", "extracting", "extracted", "needs_ocr", "failed",
)  # fmt: skip
_NEW_STATUSES = (*_OLD_STATUSES, "approved", "needs_review")


def _recreate_status_check(statuses: Sequence[str]) -> None:
    values = ", ".join(f"'{status}'" for status in statuses)
    op.drop_constraint("ck_documents_status", "documents", type_="check")
    op.create_check_constraint("ck_documents_status", "documents", f"status IN ({values})")


def upgrade() -> None:
    op.add_column("extractions", sa.Column("routing_decision", sa.String(length=32), nullable=True))
    op.add_column("extractions", sa.Column("confidence_model_version", sa.Integer(), nullable=True))
    _recreate_status_check(sorted(_NEW_STATUSES))


def downgrade() -> None:
    # Rows already routed would violate the narrower constraint, so fold them
    # back to the state they were in before routing ran. `extracted` is the
    # correct pre-routing state for both outcomes, and this is re-runnable.
    op.execute(
        sa.text(
            "UPDATE documents SET status = 'extracted' WHERE status IN ('approved', 'needs_review')"
        )
    )
    _recreate_status_check(sorted(_OLD_STATUSES))
    op.drop_column("extractions", "confidence_model_version")
    op.drop_column("extractions", "routing_decision")
