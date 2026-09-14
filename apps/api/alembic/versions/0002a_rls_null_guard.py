"""rls_null_guard: NULLIF-guard the tenant_isolation policy on rls_probe

enable_rls() now wraps current_setting() in NULLIF(..., '') so that a
pooled connection which has already served a tenant request -- where the
app.current_org_id placeholder reverts to '' rather than NULL once its
setting transaction commits -- still fails closed to zero rows instead of
raising `invalid input syntax for type uuid: ""`. That change to
enable_rls() only affects tables created after it; rls_probe's policy was
already applied by 0002_rls_probe with the old expression, so it must be
dropped and recreated here explicitly.

Revision ID: 0002a_rls_null_guard
Revises: 0002_rls_probe
"""

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0002a_rls_null_guard"
down_revision = "0002_rls_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    disable_rls(op, "rls_probe")
    enable_rls(op, "rls_probe")


def downgrade() -> None:
    # Restores the pre-guard policy so downgrade is a true inverse. Not
    # reusing enable_rls() here since it now always emits the guarded form.
    disable_rls(op, "rls_probe")
    op.execute("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON rls_probe "
        "USING (organization_id = current_setting('app.current_org_id', true)::uuid) "
        "WITH CHECK (organization_id = current_setting('app.current_org_id', true)::uuid)"
    )
