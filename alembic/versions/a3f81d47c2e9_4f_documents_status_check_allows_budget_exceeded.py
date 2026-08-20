"""4f: the status CHECK never learned about budget_exceeded

A latent, production-only bug, found by the 4f pre-flight sweep rather than by
any test.

`DocumentStatus.BUDGET_EXCEEDED` was added to the enum in Phase 4a, and
`_pause_on_budget()` writes it whenever a tenant hits their cap. The CHECK
constraint on `documents.status` was last rewritten in `7efe6f688a88`
(routing's `approved` / `needs_review`) and never gained the new value, so that
write raises CheckViolation.

The consequence is worse than a failed update. The write happens inside the
extraction transaction, so the whole thing rolls back, the message is never
acknowledged, it redelivers, it fails identically, and after `max_receive_count`
the document lands in the DLQ. The budget cap — the feature whose entire
purpose is to pause a document *safely* and resume it when the cap is raised —
would instead have destroyed exactly the documents it was protecting, and done
it silently, three retries at a time.

Nothing caught it because every budget test asserts on the enum
(`DocumentStatus.BUDGET_EXCEEDED == "budget_exceeded"`) or on the spend
counter. None of them wrote the status to a database.
`tests/test_schema_contract.py` now writes every enum value for real, so the
class cannot come back.

Revision ID: a3f81d47c2e9
Revises: f7c3e9a10b45
Create Date: 2026-08-19 16:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f81d47c2e9"
down_revision: str | Sequence[str] | None = "f7c3e9a10b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = (
    "received", "parsing", "parsed", "extracting", "extracted",
    "needs_ocr", "failed", "approved", "needs_review",
)  # fmt: skip
_NEW_STATUSES = (*_OLD_STATUSES, "budget_exceeded")


def _recreate_status_check(statuses: Sequence[str]) -> None:
    values = ", ".join(f"'{status}'" for status in statuses)
    op.drop_constraint("ck_documents_status", "documents", type_="check")
    op.create_check_constraint("ck_documents_status", "documents", f"status IN ({values})")


def upgrade() -> None:
    _recreate_status_check(sorted(_NEW_STATUSES))


def downgrade() -> None:
    # Rows paused on budget would violate the narrower constraint. Fold them to
    # `parsed`, which is where the pipeline picks them up again — the same
    # resumable point the pause was meant to hold them at, and the correct
    # pre-4a state. Re-runnable, and it loses no work: nothing has been
    # extracted for a document that stopped before the model call.
    op.execute(sa.text("UPDATE documents SET status = 'parsed' WHERE status = 'budget_exceeded'"))
    _recreate_status_check(sorted(_OLD_STATUSES))
