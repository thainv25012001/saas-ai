"""skip unsure supplier reviews

Revision ID: 0021_skip_unsure_reviews
Revises: 0020_supplier_review_fixes

Emails the classifier could not place are now ignored instead of queued for an
owner's review (`classify_supplier_email`). This retires the reviews already
queued for that reason, so the dashboard's "Emails that need attention" list
follows the new rule. The 0.85 threshold is frozen here at the value the rule
shipped with, not read from the app, so this migration keeps its meaning.

A queued review whose sender two configured suppliers both claimed is stored
the same way and is retired too. That case is rare, and the next email from
such a sender is reviewed again.
"""

from alembic import op

revision = "0021_skip_unsure_reviews"
down_revision = "0020_supplier_review_fixes"
branch_labels = None
depends_on = None

SKIP_UNSURE_REVIEWS = """
UPDATE supplier_messages
SET status = 'ignored',
    error_code = NULL,
    processed_at = COALESCE(processed_at, now()),
    updated_at = now()
WHERE status = 'review_required'
  AND error_code = 'supplier_classification_review'
  AND (classification = 'uncertain' OR classification_confidence < 0.85)
"""


def upgrade() -> None:
    op.execute(SKIP_UNSURE_REVIEWS)


def downgrade() -> None:
    # Nothing to restore: these rows cannot be told apart from emails ignored
    # later under the same rule, and an ignored email is simply not shown.
    pass
