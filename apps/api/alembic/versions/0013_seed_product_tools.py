"""seed product tools, backfill agent_tools, add message_citations.product_id

Revision ID: 0013_seed_product_tools
Revises: 0012_product_imports

Task 5 (docs/PHASE-5.md §6, §7.2): `search_products` and `get_product`, the
two tools that make Phase 5's products reachable at all. Named
`0011_seed_product_tools` in the original plan; `0011` and `0012` were
already taken by Task 2 (`product_embedding_model`) and Task 3
(`product_imports`) by the time this task started, so this migration is
`0013` -- the actual next free number, not the plan's stale one.

**Ruling 2 (task-5-brief.md): three places, together, or the tool is
inert.** Phase 4 shipped exactly this failure once already -- reviewed,
tested, correct tool code that no agent could ever reach, because the
`tools` rows existed in no migration, no seed and no API. This migration is
one of the three required places for both new tools (the other two:
`app/db/builtin_tools.DEFAULT_ENABLED_TOOL_NAMES`, which this migration's
own backfill statement reads, and `app/tools/runtime.BUILTIN_TOOL_CLASSES`,
which is Python-only and cannot be expressed in a migration at all -- see
that module for the third leg). Seeding the `tools` rows without also
updating `DEFAULT_ENABLED_TOOL_NAMES` would repeat Phase 4's mistake in a
new shape: the rows would exist, `AgentService.create_agent` would not link
them, and every test asserting on row counts (`SELECT * FROM tools`) would
pass while every agent remained unable to call either tool.

**Ruling 1 (task-5-brief.md): both tools are ON by default, unlike
`create_lead`.** `create_lead` stays off by default because it *writes* a
real `leads` row into the exact table the dashboard presents as a customer
pipeline -- ordinary playground testing would pollute it. Neither product
tool writes anything; both are reads exactly like `retrieve_knowledge`, so
the risk that justifies `create_lead`'s exception does not apply, and
defaulting them off would just be Phase 4's inertness paid for a second
time. Both are seeded here, both added to `DEFAULT_ENABLED_TOOL_NAMES`
before this migration runs its own backfill, and both are backfilled onto
every pre-existing agent by the same idempotent statement Task 7b wrote for
`retrieve_knowledge`.

**`message_citations.product_id`.** `docs/ARCHITECTURE.md` §3.5 declares it
and §5.4 rule 3 is why: "message_citations records what was actually
retrieved, so faithfulness can be scored after the fact instead of
assumed" -- true today only for document chunks, because the column the
architecture names has never actually existed until this migration adds
it. Nullable and `ON DELETE SET NULL`, matching `chunk_id`/`document_id`'s
own shape on this table (0007_message_citations.py): a citation survives
the product it names being deleted later, for the identical reason a
citation survives a re-ingested document -- this table's whole job is to
answer "what grounded this answer" about a turn that already happened, and
a `NULL` here still says "this message was grounded on a product", which a
missing row would not.
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

revision = "0013_seed_product_tools"
down_revision = "0012_product_imports"
branch_labels = None
depends_on = None

# Literal, historical copies of `SearchProductsTool.description` /
# `GetProductTool.description` as they read when this migration was
# written -- same reasoning as 0009_seed_builtin_tools.py's identical
# choice: a migration that pulled description text from a live class
# attribute would reseed whatever the CURRENT text says on a brand-new
# environment, not what shipped when this migration was authored.
# `tests/integration/test_builtin_tools.py` pins these against the live
# class attributes so a future drift is caught by CI, not discovered in
# production.
_PRODUCT_TOOLS = [
    {
        "id": uuid7(),
        "name": "search_products",
        "type": "builtin",
        "description": (
            "Search the organization's product catalogue by keyword, semantic "
            "similarity, and/or exact filters (category, price range, attributes). "
            "Use this -- never a document chunk -- to answer what the organization "
            "sells and what it costs. Returns structured product data (price, "
            "currency, availability, attributes), each result naming the exact "
            "product row it came from, or an explicit message when nothing matches "
            "the filters. Results are ranked, not filtered, by relevance: check each "
            "result's match-quality signal before treating it as clearly relevant."
        ),
    },
    {
        "id": uuid7(),
        "name": "get_product",
        "type": "builtin",
        "description": (
            "Fetch the full record for one product by its id -- price, currency, "
            "availability, stock, attributes and description. Use this to confirm "
            "or expand on a product `search_products` already surfaced, never a "
            "document chunk, for any fact this tool can report."
        ),
    },
]


def upgrade() -> None:
    op.add_column(
        "message_citations",
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    bind = op.get_bind()

    # Idempotent per row, identical mechanism to 0009_seed_builtin_tools.py.
    bind.execute(sa.text(seed_tools_sql()), _PRODUCT_TOOLS)

    # Backfill EVERY agent (pre-existing and already-migrated alike) onto
    # the full current default set, not just the two new names: harmless
    # for an agent that already has `retrieve_knowledge` (agent_tools' own
    # primary key absorbs the repeat), and exactly what makes a pre-Phase-5
    # agent gain the two new tools the same way 0009 gave every
    # pre-Task-7b agent `retrieve_knowledge`.
    bind.execute(
        sa.text(default_agent_tools_backfill_sql()),
        {"names": list(DEFAULT_ENABLED_TOOL_NAMES)},
    )


def downgrade() -> None:
    # No separate DELETE against `agent_tools`: `tool_id` is
    # `ON DELETE CASCADE` to `tools.id` (0008_tools_and_leads.py), so
    # removing the two global rows below cascades away every link this
    # migration backfilled, and every link `AgentService.create_agent` made
    # to the same global rows since -- identical reasoning to
    # 0009_seed_builtin_tools.py's downgrade.
    op.execute(
        "DELETE FROM tools WHERE organization_id IS NULL "
        "AND name IN ('search_products', 'get_product')"
    )
    op.drop_column("message_citations", "product_id")
