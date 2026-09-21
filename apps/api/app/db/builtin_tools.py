"""Shared, testable pieces of Task 7b's default-tool wiring.

`AgentService.create_agent` and `alembic/versions/0009_seed_builtin_tools.py`
both need to answer "which builtins does an agent with no operator-chosen
links start with?" -- the first for a brand-new agent, the second when
backfilling an agent that predates the migration. Both call into this module
so the two can never drift apart: a new agent and a migration-backfilled
legacy agent always end up in the identical `agent_tools` state.

Deliberately NOT the home for the seeded `tools` rows' own name/type/
description data: that is copied into the migration as literal, hardcoded
values on purpose (see that migration's module docstring) -- a migration
that pulled description text from a live class attribute would reseed
whatever the CURRENT text says on a brand-new environment, not what shipped
when the migration was authored, which is exactly the kind of drift a
migration is supposed to be immune to. This module is structural only
(which tool *names* get linked, never what they say), so sharing it between
a migration and runtime code carries none of that risk.
"""

from collections.abc import Sequence

# See task-7b-report.md for the argument. `retrieve_knowledge` is a pure
# read with no risk, so it is linked, enabled, for every agent by default.
# `create_lead` writes a real `leads` row on every call, and today the only
# thing that can call it is an authenticated org member testing their own
# agent in the playground (`POST /api/v1/chat/stream` requires a bearer
# token; `widget`/`api` channels exist only as unused enum values) -- so
# defaulting it on would let ordinary "let me try asking about pricing"
# playground turns write rows into the very `leads` table Task 8 presents
# to that same staff as a customer pipeline, indistinguishable from a real
# lead. (It will also matter for the reason this comment used to give as
# the present-tense one -- an anonymous, unauthenticated visitor -- once a
# public channel exists; that is a Phase 7 concern, not a current one.)
# Phase 4 ships no dashboard/GraphQL surface yet to review or disable it
# per-agent, so it stays reachable (the same `agent_tools` insert
# `tests/conftest.py::enable_builtin_tool` already uses) but off by
# default.
DEFAULT_ENABLED_TOOL_NAMES: Sequence[str] = ("retrieve_knowledge",)


def seed_tools_sql() -> str:
    """The idempotent statement that inserts one global (`organization_id
    IS NULL`) `tools` row, guarded by `uq_tool_global_name` (the partial
    unique index `alembic/versions/0008_tools_and_leads.py` created) so a
    second run with the same `name` is a no-op rather than a constraint
    violation. Takes `id`, `name`, `type`, `description` as bound params;
    `config` is always `{}` and `is_enabled` always `true` for a freshly
    seeded builtin.
    """
    return (
        "INSERT INTO tools (id, organization_id, name, type, description, config, is_enabled) "
        "VALUES (:id, NULL, :name, :type, :description, '{}'::jsonb, true) "
        "ON CONFLICT (name) WHERE organization_id IS NULL DO NOTHING"
    )


def default_agent_tools_backfill_sql() -> str:
    """The idempotent statement that links every agent to every currently-
    seeded global builtin named in `DEFAULT_ENABLED_TOOL_NAMES`, guarded by
    `agent_tools`'s own primary key (`agent_id`, `tool_id`) so re-running it
    -- for an agent that already has the link, from a prior run of this same
    migration, or from `AgentService.create_agent` having already linked it
    at creation time -- is a no-op rather than a constraint violation.
    Takes `names` (bound as `:names`, a list of tool names) as a param
    rather than hard-coding `DEFAULT_ENABLED_TOOL_NAMES` into the SQL text,
    so a caller can pass exactly what this module currently exports without
    the two ever being able to disagree.
    """
    return (
        "INSERT INTO agent_tools (agent_id, tool_id, organization_id, is_enabled, overrides) "
        "SELECT a.id, t.id, a.organization_id, true, '{}'::jsonb "
        "FROM agents a "
        "CROSS JOIN tools t "
        "WHERE t.organization_id IS NULL AND t.type = 'builtin' AND t.name = ANY(:names) "
        "ON CONFLICT (agent_id, tool_id) DO NOTHING"
    )
