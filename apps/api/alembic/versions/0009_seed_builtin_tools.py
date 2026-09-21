"""seed builtin tools, backfill agent_tools, drop the dead enabled_tool_names

Revision ID: 0009_seed_builtin_tools
Revises: 0008_tools_and_leads

Task 7b's review (`task-7b-brief.md`) found Phase 4 shipped *inert*: on a
freshly migrated database `tools` and `agent_tools` both had zero rows,
nothing but a test helper could create them, and an agent created the normal
way reached the provider with `tools=None`. This migration is the "make it
reachable" half of the fix -- the other half is
`app/agents/service.py::AgentService.create_agent`, which links a new
agent's defaults in the same flush as its `AgentConfig` row, using the same
`DEFAULT_ENABLED_TOOL_NAMES` this migration backfills onto agents that
predate it.

**`enabled_tool_names`.** `docs/ARCHITECTURE.md` §5.1 named
`agent_configs.enabled_tool_names` as *the* tool-resolution source; in fact
`ChatService._resolve_enabled_tool_names` has always resolved from
`agent_tools` joined to `tools` (Task 7), and nothing anywhere reads the
column. A writable field wired to nothing over GraphQL
(`updateAgentConfig`) is worse than no field, because it tells an operator
they configured something they did not -- so it is removed here, not just
hidden from the API. `agent_tools` is kept as the sole resolution source: it
already carries per-agent `is_enabled` and `overrides`, and the shadowing
semantics between an org-scoped `tools` row and a global builtin of the same
name Task 7 implemented and tested. Translating `enabled_tool_names` writes
into `agent_tools` rows instead was the other option Task 7b's brief
offered; rejected because nothing in this codebase's hand-written frontend
ever reads or writes `enabledToolNames` (only the generated GraphQL types
do), so removal breaks no real contract, while translation would have to
invent lossy answers to questions `agent_tools`'s richer shape already
settles cleanly -- does a bare name auto-vivify an org-scoped row or toggle
an existing link, and what happens to a name's `overrides` when the flat
list omits it on a later write. See `docs/ARCHITECTURE.md` §5.1 and
task-7b-report.md for the full argument.

**Why only `retrieve_knowledge` is in `DEFAULT_ENABLED_TOOL_NAMES`.**
`retrieve_knowledge` is a pure read with no side effect, so every agent gets
it. `create_lead` writes a real `leads` row every time it runs, and the risk
that matters *today* is not an anonymous public visitor -- no public channel
exists yet: `POST /api/v1/chat/stream` requires an authenticated bearer
token, the route hardcodes `channel=PLAYGROUND`, and `widget`/`api` are
unused enum values reserved for later phases. The only thing that can
trigger `create_lead` right now is an org member testing their own agent in
the playground, and defaulting it on would let an ordinary "let me try
asking about pricing" test turn into a row in the exact `leads` table
Task 8 is about to present to that same org as its customer pipeline --
test-data pollution indistinguishable from a real lead. (The anonymous-
visitor concern is real too, but only once a public channel ships in a
later phase -- it does not describe the system as it exists now.) It stays
off until an operator turns it on for that agent, which Task 8's agent
detail page (and the `setAgentToolEnabled` mutation behind it) is the
supported way to do; see `app/db/builtin_tools.py`'s module-level comment
and task-7b-report.md for the full argument.

**Tool descriptions are copied here as literal text**, not imported from
`app.tools.retrieve`/`app.tools.leads` (the code `RetrieveKnowledgeTool`/
`CreateLeadTool` actually define `description` on), on purpose: a migration
seeding data from a live class attribute would reseed whatever the CURRENT
text says on a brand-new environment running this migration from scratch,
not what shipped when this migration was authored -- exactly the kind of
drift a migration is supposed to be immune to. The two are still guaranteed
not to have drifted *at authoring time*:
`tests/integration/test_builtin_tools.py` asserts the seeded description
equals the live class attribute, so CI catches the moment they diverge
rather than trusting a comment.

**Backfill, not an implicit fallback.** An agent created before this
migration has zero `agent_tools` rows, same as a brand-new one would without
the INSERT below. Two ways to make it "just work" were on the table: treat
"no links" as "every enabled builtin" in `_resolve_enabled_tool_names`
itself, or backfill real rows here. The backfill was chosen because the
implicit-fallback reading has a real cost the brief calls out directly: it
cannot distinguish a genuinely pre-migration agent from one whose operator
deliberately unlinked every tool, and would silently re-enable builtins for
the second case. A real `agent_tools` row makes "no tools" and "not yet
configured" the same explicit, inspectable state for every agent
regardless of when it was created, and keeps `_resolve_enabled_tool_names`
itself unchanged from Task 7 -- no new branch, no new invariant for that
function's own tests to carry.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.ids import uuid7
from app.db.builtin_tools import (
    DEFAULT_ENABLED_TOOL_NAMES,
    default_agent_tools_backfill_sql,
    seed_tools_sql,
)

revision = "0009_seed_builtin_tools"
down_revision = "0008_tools_and_leads"
branch_labels = None
depends_on = None

# Literal, historical copies of `RetrieveKnowledgeTool.description` /
# `CreateLeadTool.description` as they read when this migration was
# written -- see the module docstring for why these are not imported.
# `tests/integration/test_builtin_tools.py` pins them against the live
# class attributes so a change to either without a matching migration is
# caught by CI, not discovered in production.
_BUILTIN_TOOLS = [
    {
        "id": uuid7(),
        "name": "retrieve_knowledge",
        "type": "builtin",
        "description": (
            "Search the organization's own documents for information relevant to a "
            "question. Call this when answering requires company-specific knowledge "
            "(policies, products, pricing, procedures) rather than general conversation. "
            "Returns ranked passages with citations, or an explicit message when nothing "
            "relevant is found."
        ),
    },
    {
        "id": uuid7(),
        "name": "create_lead",
        "type": "builtin",
        "description": (
            "Record a prospective customer's contact details and what they're interested in. "
            "Requires a name and at least one of email or phone -- ask for whichever is "
            "missing before calling this. Confirm the details back to the visitor first: "
            "this writes a real record that a human will follow up on."
        ),
    },
]


def upgrade() -> None:
    bind = op.get_bind()

    # Idempotent per row: a second run of this migration (or a fresh
    # `upgrade head` after `downgrade base` -> `upgrade head`) hits
    # `uq_tool_global_name` and does nothing for a name already seeded.
    bind.execute(sa.text(seed_tools_sql()), _BUILTIN_TOOLS)

    # Backfill every agent that predates this migration (and, harmlessly,
    # re-affirm the link for one `AgentService.create_agent` already made
    # for an agent created after `tools` was seeded but before this
    # specific backfill ran) onto the DEFAULT_ENABLED_TOOL_NAMES builtins.
    # Idempotent per (agent_id, tool_id): agent_tools' own primary key.
    bind.execute(
        sa.text(default_agent_tools_backfill_sql()),
        {"names": list(DEFAULT_ENABLED_TOOL_NAMES)},
    )

    # `agent_configs.enabled_tool_names` -- read by nothing (see module
    # docstring). Dropped, not just hidden from GraphQL, so no future
    # caller can be misled into believing it does anything.
    op.drop_column("agent_configs", "enabled_tool_names")


def downgrade() -> None:
    op.add_column(
        "agent_configs",
        sa.Column(
            "enabled_tool_names",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )

    # No separate DELETE against `agent_tools`: `tool_id` is
    # `ON DELETE CASCADE` to `tools.id` (migration 0008), so removing the
    # two global rows below cascades away every link this migration
    # backfilled, and every link `AgentService.create_agent` made to the
    # same global rows since -- correct for a downgrade, which is meant to
    # undo the "tools seeded" state wholesale, not just this migration's
    # own inserts.
    op.execute(
        "DELETE FROM tools WHERE organization_id IS NULL "
        "AND name IN ('retrieve_knowledge', 'create_lead')"
    )
