"""extraction confidence signals

Adds the raw scorer input vector to extractions. The score alone would force
a full reprocess every time the confidence weights change; keeping the signals
lets the calibration study refit offline against rows already in the table.

Revision ID: 0b38442255b5
Revises: c9fc32942b1d
Create Date: 2026-08-17 18:28:20.194895
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0b38442255b5"
down_revision: str | Sequence[str] | None = "c9fc32942b1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extractions",
        sa.Column("confidence_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extractions", "confidence_signals")
