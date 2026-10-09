"""move agents off the fake provider

Revision ID: 0022_agents_off_fake
Revises: 0021_skip_unsure_reviews

The offline `fake` provider is gone from the app, so an agent still on it would
fail every chat with an unknown-provider error. This moves those agents to
OpenRouter's default free model. The model id is frozen here at the value
`DEFAULT_MODELS["openrouter"]` had when this shipped, not read from the app, so
this migration keeps its meaning when that default changes.

Messages, usage events and evaluation runs that recorded `fake` are history and
are left alone.
"""

from alembic import op

revision = "0022_agents_off_fake"
down_revision = "0021_skip_unsure_reviews"
branch_labels = None
depends_on = None

MOVE_FAKE_AGENTS = """
UPDATE agents
SET provider = 'openrouter',
    model = 'google/gemma-4-31b-it:free',
    updated_at = now()
WHERE provider = 'fake'
"""


def upgrade() -> None:
    op.execute(MOVE_FAKE_AGENTS)


def downgrade() -> None:
    # Nothing to restore: once moved, nothing records which agents were on
    # `fake`, and the app can no longer serve that provider anyway.
    pass
