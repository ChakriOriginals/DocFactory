"""reaper gives up: documents.reap_count

The reaper re-enqueued a stranded document on `status NOT IN TERMINAL AND
updated_at < cutoff` and nothing else. It wrote nothing to the row, so a
document that failed at a site no handler guarded -- `put_object` of the parsed
text, which an S3 permission or KMS problem on the parsed/ prefix reaches --
stayed at `parsing` through every delivery and was taken again on every sweep,
forever, leaving one fresh DLQ copy per lap.

The obvious fix, marking the document FAILED on its last delivery whatever the
cause, trades that loop for data loss: three deliveries on a 10-second
visibility timeout is a 30-second runway, so a 40-second storage blip would kill
a document permanently, and neither the reaper nor the DLQ redrive touches a
FAILED row. Transient failures are exactly what the reaper exists to recover.

So the count lives here instead. Each rescue by the worker's healer is claimed
atomically and counted on the row, and past `max_reap_attempts` the healer
marks the document FAILED with the reason. A blip gets recovered on the next
sweep; a document that fails the same way through every rescue ends, visibly.

Revision ID: 87e29435cba3
Revises: a3f81d47c2e9
Create Date: 2026-09-15

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "87e29435cba3"
down_revision: str | Sequence[str] | None = "a3f81d47c2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default so existing rows start at zero rescues, which is true: the
    # reaper never counted anything before this revision.
    op.add_column(
        "documents",
        sa.Column("reap_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("documents", "reap_count")
