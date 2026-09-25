# Prompts Dashboard and Agent–Prompt Linking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Prompts placeholder with a working section (create, version, activate/roll back, see which agents use a prompt) and let an agent be linked to a prompt from its detail page.

**Architecture:** The prompt backend already exists (`PromptService`, GraphQL `prompts`/`prompt`/`createPrompt`/`createPromptVersion`/`activatePromptVersion`). This adds one mutation (`setAgentPrompt`), one batched field (`Prompt.agents`) and one query (`defaultSystemPrompt`) to the API, then builds two Next.js pages and one agent-page card out of presentational components that the pages feed through urql.

**Tech Stack:** FastAPI + Strawberry GraphQL + SQLAlchemy 2 async (apps/api, pytest + anyio); Next.js 15 + React 19 + urql + graphql-codegen (apps/web, vitest + Testing Library + happy-dom).

**Spec:** `docs/superpowers/specs/2026-09-25-prompts-dashboard-design.md`

## Global Constraints

- Nothing outside `globals.css` names a colour — use semantic utilities only (`text-ink-muted`, `bg-surface`, …). See `docs/DESIGN.md`.
- Every dropdown is `ui/Select`; every labelled control goes through `ui/Field`'s render prop; submits live in `CardFooter`.
- Every service lookup carries an explicit `organization_id` predicate in addition to RLS. A foreign id is `NotFoundError`, indistinguishable from an unknown one.
- `promptId: null` on `setAgentPrompt` means unlink. The prompt must be resolved via `PromptService.get_prompt` before it is written — FK checks are not subject to RLS.
- A new version is always created **inactive**; activation is a separate act.
- Host-run API tests need `DATABASE_URL`/`REDIS_URL` pointed at `localhost` (the `.env` uses docker hostnames), and must connect as the app role, not the `postgres` superuser, or RLS is silently bypassed.
- After any GraphQL schema change: `make schema` then `make web-codegen`; commit `packages/shared/schema.graphql` and `apps/web/src/graphql/generated.ts`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **Another organization's prompt id passed to `setAgentPrompt`** — must be `NotFoundError` and leave the agent's `prompt_id` unchanged (Task 1 test `test_set_prompt_rejects_another_organizations_prompt`).
2. **Unlinking then chatting** — an agent set back to `null` must answer on the default prompt and record no version (Task 1 test `test_set_prompt_none_unlinks_and_chat_falls_back_to_default`).
3. **A prompt name with no slug-able characters** (e.g. `"!!!"`) — the derived key must be empty rather than `"_"`, so the form blocks submit instead of sending an invalid key (Task 3 test in `prompts.test.ts`).
4. **Editing the new-version text back to exactly the base text** — Save draft must be disabled again, so no duplicate version is created (Task 5 `NewVersionForm` test).
5. **The agent page's prompt list failing to load** — the card must say so and not show "Built-in default" as if it were the saved value for a linked agent (Task 6 `AgentPromptCard` test).

---

## File Structure

API (`apps/api`):
- Modify `app/agents/service.py` — add `set_prompt`, `agents_by_prompt`.
- Modify `app/graphql/context.py` — add `prompt_agents_loader`.
- Modify `app/graphql/types.py` — add `Prompt.agents`.
- Modify `app/graphql/resolvers.py` — add `defaultSystemPrompt` query, `setAgentPrompt` mutation.
- Modify `packages/shared/schema.graphql` (generated).
- Create `tests/integration/test_agent_prompt_link.py` — service tests.
- Modify `tests/integration/test_graphql.py` — GraphQL tests.

Web (`apps/web/src`):
- Modify `graphql/operations.graphql`, `graphql/generated.ts` (generated).
- Create `lib/prompts.ts` + `lib/prompts.test.ts` — pure helpers.
- Create `components/prompts/PromptList.tsx` (+ test) — list rows.
- Create `components/prompts/CreatePromptForm.tsx` (+ test).
- Create `components/prompts/VersionList.tsx` (+ test).
- Create `components/prompts/VersionView.tsx` (+ test) — selected version and activation.
- Create `components/prompts/NewVersionForm.tsx` (+ test).
- Create `components/agents/AgentPromptCard.tsx` (+ test).
- Replace `app/dashboard/prompts/page.tsx`; create `app/dashboard/prompts/[id]/page.tsx`.
- Modify `app/dashboard/agents/[id]/page.tsx` — mount the card.
- Modify `components/shell/nav.ts`, `components/shell/nav.test.ts`.
- Delete `components/PlaceholderPage.tsx` if nothing else imports it.

Docs: `docs/DESIGN.md`, `docs/PHASE-6.md`, `README.md`.

---

### Task 1: `AgentService.set_prompt` and `agents_by_prompt`

**Files:**
- Modify: `apps/api/app/agents/service.py`
- Test: `apps/api/tests/integration/test_agent_prompt_link.py`

**Interfaces:**
- Produces: `AgentService.set_prompt(agent_id: uuid.UUID, prompt_id: uuid.UUID | None) -> Agent`; `AgentService.agents_by_prompt(prompt_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[Agent]]` (every requested id present, agents ordered by name).

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/integration/test_agent_prompt_link.py
import pytest

from app.agents.service import AgentService
from app.chat.service import ChatMessageStart, ChatService
from app.conversations.service import ConversationService
from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.llm.fake_provider import FakeProvider
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _prompt(session, tenant, key="sales_system", text="LINKED MARKER {{company_name}}"):
    return await PromptService(session, tenant).create_prompt(
        CreatePromptInput(name=f"Prompt {key}", key=key, system_prompt=text)
    )


async def test_set_prompt_links_an_agent_to_its_organizations_prompt(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        prompt = await _prompt(session, tenant_a)
        updated = await service.set_prompt(agent.id, prompt.id)
    assert updated.prompt_id == prompt.id


async def test_set_prompt_rejects_another_organizations_prompt(tenant_a, tenant_b):
    async with tenant_session(tenant_b) as session:
        foreign = await _prompt(session, tenant_b, key="foreign")
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        with pytest.raises(NotFoundError):
            await service.set_prompt(agent.id, foreign.id)
        reloaded = await service.get_agent(agent.id)
    assert reloaded.prompt_id is None


async def test_set_prompt_on_another_organizations_agent_is_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_b) as session:
        foreign_agent = await AgentService(session, tenant_b).create_agent(agent_input("Theirs"))
    async with tenant_session(tenant_a) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_a).set_prompt(foreign_agent.id, None)


async def test_linking_reaches_chat_and_records_the_active_version(tenant_a):
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        prompt = await _prompt(session, tenant_a)
        await service.set_prompt(agent.id, prompt.id)
        active = await PromptService(session, tenant_a).active_version(prompt.id)

        chat = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in chat.send(agent.id, "Hello")]

    assert provider.last_request is not None
    assert "LINKED MARKER" in provider.last_request.system
    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].prompt_version_id == active.id


async def test_set_prompt_none_unlinks_and_chat_falls_back_to_default(tenant_a):
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        prompt = await _prompt(session, tenant_a)
        await service.set_prompt(agent.id, prompt.id)
        updated = await service.set_prompt(agent.id, None)

        chat = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in chat.send(agent.id, "Hello")]

    assert updated.prompt_id is None
    assert provider.last_request is not None
    assert "LINKED MARKER" not in provider.last_request.system
    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].prompt_version_id is None


async def test_agents_by_prompt_groups_agents_and_includes_every_requested_id(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        used = await _prompt(session, tenant_a, key="used")
        unused = await _prompt(session, tenant_a, key="unused")
        zed = await service.create_agent(agent_input("Zed Bot"))
        abe = await service.create_agent(agent_input("Abe Bot"))
        await service.create_agent(agent_input("Unlinked Bot"))
        await service.set_prompt(zed.id, used.id)
        await service.set_prompt(abe.id, used.id)

        by_prompt = await service.agents_by_prompt([used.id, unused.id])

    assert [a.name for a in by_prompt[used.id]] == ["Abe Bot", "Zed Bot"]
    assert by_prompt[unused.id] == []
```

The history lookup (`history[1]` is the assistant message) is the one `test_chat_service.py::test_system_prompt_uses_the_active_prompt_version_not_an_older_one` uses.

- [ ] **Step 2: Run tests to verify they fail**

Run (from `apps/api`, with localhost overrides): `uv run pytest tests/integration/test_agent_prompt_link.py -v`
Expected: FAIL with `AttributeError: 'AgentService' object has no attribute 'set_prompt'`.

- [ ] **Step 3: Implement**

In `apps/api/app/agents/service.py` add imports `from collections.abc import Sequence` and `from app.prompts.service import PromptService` (check the existing import block first; `Agent` and `select` are already imported). Add after `update_agent`:

```python
    async def set_prompt(self, agent_id: uuid.UUID, prompt_id: uuid.UUID | None) -> Agent:
        """Link the agent to one of this organization's prompts, or unlink it
        with `None` (the agent then answers on the built-in default).

        The prompt is resolved through `PromptService.get_prompt` before it is
        written, and that lookup is load-bearing: `agents.prompt_id` is a plain
        foreign key, and Postgres checks a foreign key without RLS, so the
        constraint alone would accept another organization's prompt id. A
        foreign id is `NotFoundError`, as an unknown one is."""
        agent = await self.get_agent(agent_id)
        if prompt_id is not None:
            await PromptService(self.session, self.tenant).get_prompt(prompt_id)
        agent.prompt_id = prompt_id
        await self.session.flush()
        # Same `updated_at` reload as `update_agent`.
        await self.session.refresh(agent)
        return agent

    async def agents_by_prompt(
        self, prompt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[Agent]]:
        """Agents linked to each of several prompts, by name, keyed by prompt
        id with every requested id present. The batched form behind
        `Prompt.agents`' dataloader; like `PromptService.versions_by_prompt`
        it does not raise for an unknown or foreign id."""
        by_prompt: dict[uuid.UUID, list[Agent]] = {pid: [] for pid in prompt_ids}
        if not prompt_ids:
            return by_prompt
        result = await self.session.execute(
            select(Agent)
            .where(
                Agent.prompt_id.in_(list(prompt_ids)),
                Agent.organization_id == self.tenant.organization_id,
            )
            .order_by(Agent.name)
        )
        for agent in result.scalars().all():
            assert agent.prompt_id is not None
            by_prompt[agent.prompt_id].append(agent)
        return by_prompt
```

If importing `PromptService` at module top creates a cycle (run the tests to see), import it inside `set_prompt` instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_agent_prompt_link.py tests/integration/test_agent_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agents/service.py apps/api/tests/integration/test_agent_prompt_link.py
git commit -m "feat(agents): link an agent to one of its organization's prompts"
```

---

### Task 2: GraphQL — `setAgentPrompt`, `Prompt.agents`, `defaultSystemPrompt`

**Files:**
- Modify: `apps/api/app/graphql/context.py`, `apps/api/app/graphql/types.py`, `apps/api/app/graphql/resolvers.py`
- Modify (generated): `packages/shared/schema.graphql`
- Test: `apps/api/tests/integration/test_graphql.py`

**Interfaces:**
- Consumes: `AgentService.set_prompt`, `AgentService.agents_by_prompt` (Task 1).
- Produces (GraphQL): `mutation setAgentPrompt(agentId: UUID!, promptId: UUID): Agent!`; `Prompt.agents: [Agent!]!`; `query defaultSystemPrompt: String!`.

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/integration/test_graphql.py` (it already has `graphql`, `create_agent`, `auth_headers`, `_create_prompt(client, headers, key, *extra_versions) -> prompt_id`):

```python
async def test_set_agent_prompt_links_and_unlinks(client, auth_headers):
    agent = await create_agent(client, auth_headers, "Linker")
    agent_id = agent.json()["data"]["createAgent"]["id"]
    prompt_id = await _create_prompt(client, auth_headers, "linked_prompt")
    mutation = (
        "mutation S($a: UUID!, $p: UUID) { setAgentPrompt(agentId: $a, promptId: $p) { id promptId } }"
    )

    linked = await graphql(client, mutation, {"a": agent_id, "p": prompt_id}, auth_headers)
    assert linked.json()["data"]["setAgentPrompt"]["promptId"] == prompt_id

    agents = await graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { agents { id name } } }",
        {"id": prompt_id},
        auth_headers,
    )
    assert agents.json()["data"]["prompt"]["agents"] == [{"id": agent_id, "name": "Linker"}]

    unlinked = await graphql(client, mutation, {"a": agent_id, "p": None}, auth_headers)
    assert unlinked.json()["data"]["setAgentPrompt"]["promptId"] is None


async def test_set_agent_prompt_with_an_unknown_prompt_is_not_found(client, auth_headers):
    agent = await create_agent(client, auth_headers, "Unknown Linker")
    agent_id = agent.json()["data"]["createAgent"]["id"]
    response = await graphql(
        client,
        "mutation S($a: UUID!, $p: UUID) { setAgentPrompt(agentId: $a, promptId: $p) { id } }",
        {"a": agent_id, "p": "00000000-0000-7000-8000-000000000000"},
        auth_headers,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_prompts_agents_is_empty_for_an_unused_prompt(client, auth_headers):
    await _create_prompt(client, auth_headers, "unused_prompt")
    response = await graphql(client, "{ prompts { key agents { id } } }", headers=auth_headers)
    rows = {p["key"]: p["agents"] for p in response.json()["data"]["prompts"]}
    assert rows["unused_prompt"] == []


async def test_default_system_prompt_returns_the_unrendered_default(client, auth_headers):
    response = await graphql(client, "{ defaultSystemPrompt }", headers=auth_headers)
    text = response.json()["data"]["defaultSystemPrompt"]
    assert "{{company_name}}" in text
    assert "{{agent_name}}" in text


async def test_default_system_prompt_requires_authentication(client):
    response = await graphql(client, "{ defaultSystemPrompt }")
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"
```


- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_graphql.py -k "set_agent_prompt or prompts_agents or default_system_prompt" -v`
Expected: FAIL — GraphQL validation errors (`Cannot query field 'setAgentPrompt'`).

- [ ] **Step 3: Implement**

`app/graphql/context.py` — beside `prompt_versions_loader` (import `Agent` from `app.db.models` and `AgentService` from `app.agents.service` if not already imported):

```python
        # `prompts { agents }` -- which agents run each prompt -- in one query.
        self.prompt_agents_loader: DataLoader[uuid.UUID, list[Agent]] | None = (
            DataLoader(load_fn=self._load_prompt_agents) if session is not None else None
        )
```

```python
    async def _load_prompt_agents(self, prompt_ids: Sequence[uuid.UUID]) -> list[list[Agent]]:
        """Delegated to `AgentService.agents_by_prompt`, which carries the
        explicit `organization_id` predicate."""
        assert self.session is not None
        assert self.tenant is not None
        by_prompt = await AgentService(self.session, self.tenant).agents_by_prompt(prompt_ids)
        return [by_prompt[prompt_id] for prompt_id in prompt_ids]
```

`app/graphql/types.py` — in `class Prompt`, after `versions`:

```python
    @strawberry.field
    async def agents(self, info: strawberry.Info[Context, None]) -> list[Agent]:
        """The agents linked to this prompt, by name. Each runs the prompt's
        active version. Same unauthenticated guard as `versions`."""
        if info.context.prompt_agents_loader is None:
            raise AuthenticationError("authentication required")
        models = await info.context.prompt_agents_loader.load(self.id)
        return [Agent.from_model(m) for m in models]
```

`app/graphql/resolvers.py` — import `from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT`. In `Query`, after `prompt`:

```python
    @strawberry.field
    async def default_system_prompt(self, info: Info) -> str:
        """The built-in text an agent with no prompt answers on, unrendered,
        so the dashboard can start a new prompt from it."""
        _require_tenant(info)
        return DEFAULT_SALES_SYSTEM_PROMPT
```

In `Mutation`, after `delete_agent`:

```python
    @strawberry.mutation
    async def set_agent_prompt(
        self, info: Info, agent_id: uuid.UUID, prompt_id: uuid.UUID | None = None
    ) -> gql.Agent:
        """`promptId: null` unlinks. A dedicated mutation rather than a field
        on `UpdateAgentInput`, where `null` already means "unchanged"."""
        agent = await _agents(info).set_prompt(agent_id, prompt_id)
        return gql.Agent.from_model(agent)
```

- [ ] **Step 4: Run tests, export schema**

Run: `uv run pytest tests/integration/test_graphql.py -v`
Expected: PASS.
Run (repo root): `make schema`
Expected: `packages/shared/schema.graphql` now contains `setAgentPrompt`, `defaultSystemPrompt`, and `agents: [Agent!]!` under `type Prompt`.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/graphql packages/shared/schema.graphql apps/api/tests/integration/test_graphql.py
git commit -m "feat(api): GraphQL surface for agent-prompt linking"
```

---

### Task 3: Web operations, codegen, and `lib/prompts.ts`

**Files:**
- Modify: `apps/web/src/graphql/operations.graphql`, `apps/web/src/graphql/generated.ts` (generated)
- Create: `apps/web/src/lib/prompts.ts`, `apps/web/src/lib/prompts.test.ts`

**Interfaces:**
- Produces documents: `PromptsDocument`, `PromptDocument`, `DefaultSystemPromptDocument`, `CreatePromptDocument`, `CreatePromptVersionDocument`, `ActivatePromptVersionDocument`, `SetAgentPromptDocument`; `AgentDocument` gains `promptId`.
- Produces helpers:
  - `type VersionSummary = { id: string; version: number; isActive: boolean }`
  - `promptKeyFromName(name: string): string`
  - `PROMPT_KEY_PATTERN: RegExp` (`/^[a-z0-9_]+$/`)
  - `activeVersionOf<T extends VersionSummary>(versions: readonly T[]): T | null`
  - `activationLabel(selected: number, active: number | null): string`
  - `activationImpact(version: number, agentNames: readonly string[]): string`
  - `agentCountLabel(count: number): string`

- [ ] **Step 1: Add operations** — append to `operations.graphql`, and add `promptId` to the `Agent` query's selection (`id name slug status provider model temperature maxTokens promptId`):

```graphql
query Prompts {
  prompts { id name key description versions { id version isActive } agents { id } }
}

query Prompt($id: UUID!) {
  prompt(id: $id) {
    id name key description
    versions { id version systemPrompt isActive notes createdAt }
    agents { id name status }
  }
}

query DefaultSystemPrompt {
  defaultSystemPrompt
}

mutation CreatePrompt($input: CreatePromptInput!) {
  createPrompt(input: $input) { id }
}

mutation CreatePromptVersion($promptId: UUID!, $input: CreatePromptVersionInput!) {
  createPromptVersion(promptId: $promptId, input: $input) { id version isActive }
}

mutation ActivatePromptVersion($versionId: UUID!) {
  activatePromptVersion(versionId: $versionId) { id isActive }
}

mutation SetAgentPrompt($agentId: UUID!, $promptId: UUID) {
  setAgentPrompt(agentId: $agentId, promptId: $promptId) { id promptId }
}
```

Run (repo root): `make web-codegen`
Expected: `generated.ts` exports the seven new `*Document`s. Run `cd apps/web && npm run typecheck` — PASS.

- [ ] **Step 2: Write failing helper tests**

```ts
// apps/web/src/lib/prompts.test.ts
import { describe, expect, it } from "vitest";
import {
  PROMPT_KEY_PATTERN,
  activationImpact,
  activationLabel,
  activeVersionOf,
  agentCountLabel,
  promptKeyFromName,
} from "./prompts";

describe("promptKeyFromName", () => {
  it("lowercases and joins words with underscores", () => {
    expect(promptKeyFromName("Sales System Prompt")).toBe("sales_system_prompt");
  });
  it("collapses runs of other characters and trims the ends", () => {
    expect(promptKeyFromName("  Onboarding — v2!  ")).toBe("onboarding_v2");
  });
  it("is empty, not '_', for a name with nothing slug-able", () => {
    expect(promptKeyFromName("!!!")).toBe("");
  });
  it("caps at 100 characters and always matches the API pattern when non-empty", () => {
    const key = promptKeyFromName("a".repeat(150));
    expect(key).toHaveLength(100);
    expect(PROMPT_KEY_PATTERN.test(key)).toBe(true);
  });
});

describe("activeVersionOf", () => {
  it("finds the active version, or null", () => {
    const versions = [
      { id: "b", version: 2, isActive: false },
      { id: "a", version: 1, isActive: true },
    ];
    expect(activeVersionOf(versions)?.id).toBe("a");
    expect(activeVersionOf([])).toBeNull();
  });
});

describe("activationLabel", () => {
  it("calls activating an older version a rollback", () => {
    expect(activationLabel(1, 3)).toBe("Roll back to v1");
  });
  it("calls activating a newer version an activation", () => {
    expect(activationLabel(4, 3)).toBe("Activate v4");
    expect(activationLabel(1, null)).toBe("Activate v1");
  });
});

describe("activationImpact", () => {
  it("names the agents it goes live for", () => {
    expect(activationImpact(2, ["Abe"])).toBe("v2 becomes live for Abe on their next message.");
    expect(activationImpact(2, ["Abe", "Zed"])).toBe(
      "v2 becomes live for Abe and Zed on their next message.",
    );
    expect(activationImpact(2, ["Abe", "Kim", "Zed"])).toBe(
      "v2 becomes live for Abe, Kim and Zed on their next message.",
    );
  });
  it("says when no agent uses the prompt", () => {
    expect(activationImpact(2, [])).toBe("No agents use this prompt yet, so nothing changes for customers.");
  });
});

describe("agentCountLabel", () => {
  it("pluralises and names the unused case", () => {
    expect(agentCountLabel(0)).toBe("Not used");
    expect(agentCountLabel(1)).toBe("1 agent");
    expect(agentCountLabel(3)).toBe("3 agents");
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd apps/web && npx vitest run src/lib/prompts.test.ts`
Expected: FAIL — cannot resolve `./prompts`.

- [ ] **Step 4: Implement**

```ts
// apps/web/src/lib/prompts.ts

/** The API's own rule for a prompt key (`app/prompts/schemas.py`). */
export const PROMPT_KEY_PATTERN = /^[a-z0-9_]+$/;

export type VersionSummary = { id: string; version: number; isActive: boolean };

/** A key suggested from the name while the user has not typed one. Empty --
 * never "_" -- when nothing in the name survives, so the form's required
 * check stops the submit instead of the API rejecting it. */
export function promptKeyFromName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 100)
    .replace(/_+$/, "");
}

export function activeVersionOf<T extends VersionSummary>(versions: readonly T[]): T | null {
  return versions.find((version) => version.isActive) ?? null;
}

/** Activating an older version is how a prompt is rolled back, so the button
 * says so. */
export function activationLabel(selected: number, active: number | null): string {
  return active !== null && selected < active ? `Roll back to v${selected}` : `Activate v${selected}`;
}

function joinNames(names: readonly string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

export function activationImpact(version: number, agentNames: readonly string[]): string {
  if (agentNames.length === 0) return "No agents use this prompt yet, so nothing changes for customers.";
  return `v${version} becomes live for ${joinNames(agentNames)} on their next message.`;
}

export function agentCountLabel(count: number): string {
  if (count === 0) return "Not used";
  return count === 1 ? "1 agent" : `${count} agents`;
}
```

- [ ] **Step 5: Run tests** — `npx vitest run src/lib/prompts.test.ts` → PASS; `npm run typecheck` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/graphql apps/web/src/lib/prompts.ts apps/web/src/lib/prompts.test.ts
git commit -m "feat(web): prompt operations and helpers"
```

---

### Task 4: Prompts list page and create form

**Files:**
- Create: `apps/web/src/components/prompts/PromptList.tsx`, `PromptList.test.tsx`, `CreatePromptForm.tsx`, `CreatePromptForm.test.tsx`
- Replace: `apps/web/src/app/dashboard/prompts/page.tsx`

**Interfaces:**
- Consumes: `PromptsDocument`, `DefaultSystemPromptDocument`, `CreatePromptDocument`; `promptKeyFromName`, `PROMPT_KEY_PATTERN`, `activeVersionOf`, `agentCountLabel` (Task 3).
- Produces:
  - `type PromptRow = { id: string; name: string; key: string; activeVersion: number | null; versionCount: number; agentCount: number }`
  - `PromptList({ prompts }: { prompts: readonly PromptRow[] })`
  - `type NewPrompt = { name: string; key: string; description: string | null; systemPrompt: string }`
  - `CreatePromptForm({ defaultPrompt, submitting, error, onCreate }: { defaultPrompt: string | null; submitting: boolean; error: string | null; onCreate: (values: NewPrompt) => void })`

- [ ] **Step 1: Write failing component tests**

```tsx
// apps/web/src/components/prompts/PromptList.test.tsx
// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PromptList, type PromptRow } from "./PromptList";

const row: PromptRow = { id: "p1", name: "Sales", key: "sales_system", activeVersion: 3, versionCount: 4, agentCount: 2 };

describe("PromptList", () => {
  it("links each prompt and shows key, active version, count and usage", () => {
    render(<PromptList prompts={[row]} />);
    expect(screen.getByRole("link", { name: "Sales" })).toHaveAttribute("href", "/dashboard/prompts/p1");
    expect(screen.getByText("sales_system")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(screen.getByText("4 versions")).toBeInTheDocument();
    expect(screen.getByText("2 agents")).toBeInTheDocument();
  });

  it("says a prompt nobody uses is not used", () => {
    render(<PromptList prompts={[{ ...row, agentCount: 0 }]} />);
    expect(screen.getByText("Not used")).toBeInTheDocument();
  });
});
```

```tsx
// apps/web/src/components/prompts/CreatePromptForm.test.tsx
// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreatePromptForm } from "./CreatePromptForm";

function setup(overrides: Partial<React.ComponentProps<typeof CreatePromptForm>> = {}) {
  const onCreate = vi.fn();
  render(
    <CreatePromptForm defaultPrompt="Default {{company_name}}" submitting={false} error={null} onCreate={onCreate} {...overrides} />,
  );
  return onCreate;
}

describe("CreatePromptForm", () => {
  it("starts the system prompt from the built-in default", () => {
    setup();
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("Default {{company_name}}");
  });

  it("derives the key from the name until the key is edited", () => {
    setup();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Sales Prompt" } });
    expect(screen.getByLabelText(/^key/i)).toHaveValue("sales_prompt");
    fireEvent.change(screen.getByLabelText(/^key/i), { target: { value: "custom_key" } });
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Other" } });
    expect(screen.getByLabelText(/^key/i)).toHaveValue("custom_key");
  });

  it("submits the values, with an empty description as null", () => {
    const onCreate = setup();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Sales Prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "Create prompt" }));
    expect(onCreate).toHaveBeenCalledWith({
      name: "Sales Prompt",
      key: "sales_prompt",
      description: null,
      systemPrompt: "Default {{company_name}}",
    });
  });

  it("blocks a key the API would reject", () => {
    const onCreate = setup();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Sales" } });
    fireEvent.change(screen.getByLabelText(/^key/i), { target: { value: "Bad Key" } });
    fireEvent.click(screen.getByRole("button", { name: "Create prompt" }));
    expect(onCreate).not.toHaveBeenCalled();
    expect(screen.getByText(/lowercase letters, digits and underscores/i)).toBeInTheDocument();
  });

  it("shows the API's error", () => {
    setup({ error: "a prompt with key 'sales' already exists" });
    expect(screen.getByRole("alert")).toHaveTextContent("already exists");
  });
});
```

- [ ] **Step 2: Run to verify failure** — `npx vitest run src/components/prompts` → FAIL (modules missing).

- [ ] **Step 3: Implement `PromptList.tsx`**

```tsx
import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { agentCountLabel } from "@/lib/prompts";

export type PromptRow = {
  id: string;
  name: string;
  key: string;
  activeVersion: number | null;
  versionCount: number;
  agentCount: number;
};

/** Presentational: the page owns the query and maps it to rows. */
export function PromptList({ prompts }: { prompts: readonly PromptRow[] }) {
  return (
    <ul className="divide-y divide-line">
      {prompts.map((prompt) => (
        <li key={prompt.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3">
          <div className="min-w-0">
            <Link href={`/dashboard/prompts/${prompt.id}`} className="text-sm font-medium text-ink hover:underline">
              {prompt.name}
            </Link>
            <p className="font-mono text-xs text-ink-subtle">{prompt.key}</p>
          </div>
          <div className="flex shrink-0 items-center gap-3 text-xs text-ink-muted">
            {prompt.activeVersion !== null ? <Badge tone="success">{`v${prompt.activeVersion}`}</Badge> : null}
            <span>{prompt.versionCount === 1 ? "1 version" : `${prompt.versionCount} versions`}</span>
            <span>{agentCountLabel(prompt.agentCount)}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 4: Implement `CreatePromptForm.tsx`**

```tsx
"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { PROMPT_KEY_PATTERN, promptKeyFromName } from "@/lib/prompts";

export type NewPrompt = { name: string; key: string; description: string | null; systemPrompt: string };

export const PROMPT_VARIABLES_HELP =
  "{{company_name}} and {{agent_name}} are filled in when the agent answers.";

export function CreatePromptForm({
  defaultPrompt,
  submitting,
  error,
  onCreate,
}: {
  /** The built-in default text; `null` while it loads. */
  defaultPrompt: string | null;
  submitting: boolean;
  error: string | null;
  onCreate: (values: NewPrompt) => void;
}) {
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [keyEdited, setKeyEdited] = useState(false);
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState(defaultPrompt ?? "");
  const [keyError, setKeyError] = useState<string | null>(null);

  // The default arrives after first render; seed it once, and never over
  // something the user has typed.
  useEffect(() => {
    if (defaultPrompt !== null) setSystemPrompt((current) => (current === "" ? defaultPrompt : current));
  }, [defaultPrompt]);

  function onNameChange(value: string) {
    setName(value);
    if (!keyEdited) setKey(promptKeyFromName(value));
  }

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!PROMPT_KEY_PATTERN.test(key)) {
      setKeyError("Use lowercase letters, digits and underscores only.");
      return;
    }
    setKeyError(null);
    onCreate({ name: name.trim(), key, description: description.trim() || null, systemPrompt });
  }

  return (
    <form onSubmit={onSubmit}>
      <Card>
        <CardHeader title="New prompt" description="Version 1 is created and made active straight away." />
        <CardBody className="space-y-4">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Name" required>
              {(control) => (
                <Input {...control} type="text" required maxLength={255} value={name} onChange={(e) => onNameChange(e.target.value)} />
              )}
            </Field>
            <Field label="Key" description="A stable identifier, e.g. sales_system." error={keyError ?? undefined} required>
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  required
                  maxLength={100}
                  className="font-mono"
                  value={key}
                  onChange={(e) => {
                    setKeyEdited(true);
                    setKey(e.target.value);
                  }}
                />
              )}
            </Field>
          </div>
          <Field label="Description">
            {(control) => (
              <Input {...control} type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
            )}
          </Field>
          <Field label="System prompt" description={PROMPT_VARIABLES_HELP} required>
            {(control) => (
              <Textarea
                {...control}
                required
                rows={14}
                className="font-mono text-xs"
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
              />
            )}
          </Field>
        </CardBody>
        <CardFooter>
          <Button type="submit" loading={submitting} loadingLabel="Creating…">
            Create prompt
          </Button>
        </CardFooter>
      </Card>
    </form>
  );
}
```

Check `ui/Field.tsx`'s `FieldProps.error` type and `ui/Alert.tsx`'s `role` before relying on them; adapt the `error` prop shape if it is not `string | undefined`.

- [ ] **Step 5: Replace the page**

```tsx
// apps/web/src/app/dashboard/prompts/page.tsx
"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { CreatePromptForm, type NewPrompt } from "@/components/prompts/CreatePromptForm";
import { PromptList } from "@/components/prompts/PromptList";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { CreatePromptDocument, DefaultSystemPromptDocument, PromptsDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { activeVersionOf } from "@/lib/prompts";

export default function PromptsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [creating, setCreating] = useState(false);

  const [{ data, fetching, error }] = useQuery({
    query: PromptsDocument,
    pause: loading || !user,
    requestPolicy: "cache-and-network",
  });
  const [defaultResult] = useQuery({ query: DefaultSystemPromptDocument, pause: loading || !user });
  const [createResult, createPrompt] = useMutation(CreatePromptDocument);

  const rows = useMemo(
    () =>
      (data?.prompts ?? []).map((prompt) => ({
        id: prompt.id,
        name: prompt.name,
        key: prompt.key,
        activeVersion: activeVersionOf(prompt.versions)?.version ?? null,
        versionCount: prompt.versions.length,
        agentCount: prompt.agents.length,
      })),
    [data],
  );

  async function onCreate(values: NewPrompt) {
    const result = await createPrompt({ input: values });
    const id = result.data?.createPrompt.id;
    // Straight to the prompt: linking it to an agent and drafting v2 happen there.
    if (id) router.push(`/dashboard/prompts/${id}`);
  }

  const form = (
    <CreatePromptForm
      defaultPrompt={defaultResult.data?.defaultSystemPrompt ?? null}
      submitting={createResult.fetching}
      error={firstGraphQLError(createResult.error)}
      onCreate={onCreate}
    />
  );

  const queryError = firstGraphQLError(error);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Prompts"
        description="The versioned system prompts behind your agents."
        actions={
          rows.length > 0 && !creating ? (
            <Button onClick={() => setCreating(true)}>New prompt</Button>
          ) : null
        }
      />
      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}
      {fetching && !data ? (
        <LoadingState label="Loading prompts…" />
      ) : rows.length === 0 ? (
        <>
          <Card>
            <EmptyState
              icon="prompt"
              title="No prompts yet"
              description="Agents without a prompt answer on the built-in default. Create a prompt to version that text and pin it in evaluations."
            />
          </Card>
          {form}
        </>
      ) : (
        <>
          {creating ? form : null}
          <Card>
            <PromptList prompts={rows} />
          </Card>
        </>
      )}
    </div>
  );
}
```

Check `LoadingState`'s prop name in `ui/Spinner.tsx` and match it.

- [ ] **Step 6: Run tests and typecheck** — `npx vitest run src/components/prompts && npm run typecheck && npm run lint` → PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/web/src/components/prompts apps/web/src/app/dashboard/prompts/page.tsx
git commit -m "feat(web): Prompts list and create form"
```

---

### Task 5: Prompt detail page — versions, activation, new draft

**Files:**
- Create: `apps/web/src/components/prompts/VersionList.tsx` (+ test), `VersionView.tsx` (+ test), `NewVersionForm.tsx` (+ test)
- Create: `apps/web/src/app/dashboard/prompts/[id]/page.tsx`

**Interfaces:**
- Consumes: `PromptDocument`, `CreatePromptVersionDocument`, `ActivatePromptVersionDocument`; `activeVersionOf`, `activationLabel`, `activationImpact` (Task 3); `PROMPT_VARIABLES_HELP` (Task 4).
- Produces:
  - `type VersionRow = { id: string; version: number; systemPrompt: string; isActive: boolean; notes: string | null; createdAt: string }`
  - `VersionList({ versions, selectedId, onSelect }: { versions: readonly VersionRow[]; selectedId: string | null; onSelect: (id: string) => void })`
  - `VersionView({ version, activeVersion, agentNames, activating, error, onActivate }: { version: VersionRow; activeVersion: number | null; agentNames: readonly string[]; activating: boolean; error: string | null; onActivate: (id: string) => void })`
  - `NewVersionForm({ baseText, baseVersion, submitting, error, onSave }: { baseText: string; baseVersion: number; submitting: boolean; error: string | null; onSave: (values: { systemPrompt: string; notes: string | null }) => void })`

- [ ] **Step 1: Write failing tests**

```tsx
// apps/web/src/components/prompts/VersionList.test.tsx
// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VersionList, type VersionRow } from "./VersionList";

const versions: VersionRow[] = [
  { id: "v2", version: 2, systemPrompt: "two", isActive: false, notes: null, createdAt: "2026-09-25T10:00:00Z" },
  { id: "v1", version: 1, systemPrompt: "one", isActive: true, notes: "first cut", createdAt: "2026-09-24T10:00:00Z" },
];

describe("VersionList", () => {
  it("marks the active version and the selected one", () => {
    render(<VersionList versions={versions} selectedId="v2" onSelect={vi.fn()} />);
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /v2/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /v1/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("first cut")).toBeInTheDocument();
    expect(screen.getByText("No notes")).toBeInTheDocument();
  });

  it("selects a version on click", () => {
    const onSelect = vi.fn();
    render(<VersionList versions={versions} selectedId="v2" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /v1/ }));
    expect(onSelect).toHaveBeenCalledWith("v1");
  });
});
```

```tsx
// apps/web/src/components/prompts/VersionView.test.tsx
// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { VersionRow } from "./VersionList";
import { VersionView } from "./VersionView";

const v1: VersionRow = { id: "v1", version: 1, systemPrompt: "old text", isActive: false, notes: null, createdAt: "2026-09-24T10:00:00Z" };

function setup(overrides: Partial<React.ComponentProps<typeof VersionView>> = {}) {
  const onActivate = vi.fn();
  render(
    <VersionView version={v1} activeVersion={3} agentNames={["Abe", "Zed"]} activating={false} error={null} onActivate={onActivate} {...overrides} />,
  );
  return onActivate;
}

describe("VersionView", () => {
  it("shows the text read-only", () => {
    setup();
    expect(screen.getByText("old text")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("calls activating an older version a rollback, and confirms naming the agents", () => {
    const onActivate = setup();
    fireEvent.click(screen.getByRole("button", { name: "Roll back to v1" }));
    expect(onActivate).not.toHaveBeenCalled();
    expect(screen.getByText("v1 becomes live for Abe and Zed on their next message.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onActivate).toHaveBeenCalledWith("v1");
  });

  it("can back out of the confirmation", () => {
    const onActivate = setup();
    fireEvent.click(screen.getByRole("button", { name: "Roll back to v1" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onActivate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Roll back to v1" })).toBeInTheDocument();
  });

  it("offers no activation for the active version", () => {
    setup({ version: { ...v1, isActive: true }, activeVersion: 1 });
    expect(screen.queryByRole("button", { name: /activate|roll back/i })).toBeNull();
    expect(screen.getByText(/live for every agent using this prompt/i)).toBeInTheDocument();
  });
});
```

```tsx
// apps/web/src/components/prompts/NewVersionForm.test.tsx
// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NewVersionForm } from "./NewVersionForm";

function setup() {
  const onSave = vi.fn();
  render(<NewVersionForm baseText="base" baseVersion={2} submitting={false} error={null} onSave={onSave} />);
  return onSave;
}

describe("NewVersionForm", () => {
  it("starts from the base version and cannot save it unchanged", () => {
    setup();
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("base");
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
  });

  it("is disabled again when edited back to the base text", () => {
    setup();
    const text = screen.getByLabelText(/system prompt/i);
    fireEvent.change(text, { target: { value: "base changed" } });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeEnabled();
    fireEvent.change(text, { target: { value: "base" } });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
  });

  it("is disabled for blank text", () => {
    setup();
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
  });

  it("saves the text and notes", () => {
    const onSave = setup();
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "new" } });
    fireEvent.change(screen.getByLabelText(/notes/i), { target: { value: "tighter rules" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(onSave).toHaveBeenCalledWith({ systemPrompt: "new", notes: "tighter rules" });
  });
});
```

- [ ] **Step 2: Run to verify failure** — `npx vitest run src/components/prompts` → FAIL (modules missing).

- [ ] **Step 3: Implement `VersionList.tsx`**

```tsx
import { Badge } from "@/components/ui/Badge";
import { cn, focusRing } from "@/components/ui/cn";
import { formatTimestamp } from "@/lib/format";

export type VersionRow = {
  id: string;
  version: number;
  systemPrompt: string;
  isActive: boolean;
  notes: string | null;
  createdAt: string;
};

export function VersionList({
  versions,
  selectedId,
  onSelect,
}: {
  versions: readonly VersionRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <ul className="space-y-1 p-2">
      {versions.map((version) => (
        <li key={version.id}>
          <button
            type="button"
            aria-pressed={version.id === selectedId}
            onClick={() => onSelect(version.id)}
            className={cn(
              "w-full rounded-control px-3 py-2 text-left hover:bg-surface-muted",
              version.id === selectedId && "bg-surface-muted",
              focusRing,
            )}
          >
            <span className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">{`v${version.version}`}</span>
              {version.isActive ? <Badge tone="success">Active</Badge> : null}
              <span className="ml-auto text-xs text-ink-subtle">{formatTimestamp(version.createdAt)}</span>
            </span>
            <span className="mt-0.5 block truncate text-xs text-ink-muted">{version.notes ?? "No notes"}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
```

Check `ui/cn.ts` exports `focusRing` and `cn`, and whether `focusRing` needs an offset class beside it (DESIGN.md: controls use `offset-1`).

- [ ] **Step 4: Implement `VersionView.tsx`**

```tsx
"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { activationImpact, activationLabel } from "@/lib/prompts";
import type { VersionRow } from "./VersionList";

export function VersionView({
  version,
  activeVersion,
  agentNames,
  activating,
  error,
  onActivate,
}: {
  version: VersionRow;
  activeVersion: number | null;
  agentNames: readonly string[];
  activating: boolean;
  error: string | null;
  onActivate: (id: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  // A confirmation belongs to the version it was asked about.
  useEffect(() => setConfirming(false), [version.id]);

  return (
    <Card>
      <CardHeader
        title={`Version ${version.version}`}
        description={version.isActive ? "Live for every agent using this prompt." : "Not live. Evaluate it, then activate it."}
      />
      <CardBody className="space-y-3">
        {error ? <Alert tone="danger">{error}</Alert> : null}
        <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-control bg-surface-muted p-3 font-mono text-xs text-ink">
          {version.systemPrompt}
        </pre>
      </CardBody>
      {version.isActive ? null : (
        <CardFooter className="flex-wrap">
          {confirming ? (
            <>
              <p className="text-sm text-ink-muted">{activationImpact(version.version, agentNames)}</p>
              <Button onClick={() => onActivate(version.id)} loading={activating} loadingLabel="Activating…">
                Confirm
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button onClick={() => setConfirming(true)}>{activationLabel(version.version, activeVersion)}</Button>
          )}
        </CardFooter>
      )}
    </Card>
  );
}
```

- [ ] **Step 5: Implement `NewVersionForm.tsx`**

```tsx
"use client";

import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { PROMPT_VARIABLES_HELP } from "./CreatePromptForm";

/** Remount it (`key`) when the base version changes -- its state is seeded
 * from `baseText` once. */
export function NewVersionForm({
  baseText,
  baseVersion,
  submitting,
  error,
  onSave,
}: {
  baseText: string;
  baseVersion: number;
  submitting: boolean;
  error: string | null;
  onSave: (values: { systemPrompt: string; notes: string | null }) => void;
}) {
  const [text, setText] = useState(baseText);
  const [notes, setNotes] = useState("");
  const unchanged = text === baseText || text.trim() === "";

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (unchanged) return;
    onSave({ systemPrompt: text, notes: notes.trim() || null });
  }

  return (
    <form onSubmit={onSubmit}>
      <Card>
        <CardHeader
          title="New version"
          description={`Starts from v${baseVersion}. Saved as a draft: nothing changes for customers until you activate it.`}
        />
        <CardBody className="space-y-4">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <Field label="System prompt" description={PROMPT_VARIABLES_HELP} required>
            {(control) => (
              <Textarea {...control} rows={14} className="font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} />
            )}
          </Field>
          <Field label="Notes" description="What changed, for whoever reads the history next.">
            {(control) => <Input {...control} type="text" value={notes} onChange={(e) => setNotes(e.target.value)} />}
          </Field>
        </CardBody>
        <CardFooter>
          <Button type="submit" disabled={unchanged} loading={submitting} loadingLabel="Saving…">
            Save draft
          </Button>
        </CardFooter>
      </Card>
    </form>
  );
}
```

- [ ] **Step 6: Implement the page**

```tsx
// apps/web/src/app/dashboard/prompts/[id]/page.tsx
"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { useMutation, useQuery } from "urql";
import { NewVersionForm } from "@/components/prompts/NewVersionForm";
import { VersionList } from "@/components/prompts/VersionList";
import { VersionView } from "@/components/prompts/VersionView";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { ActivatePromptVersionDocument, CreatePromptVersionDocument, PromptDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { activeVersionOf } from "@/lib/prompts";

export default function PromptDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { user, loading } = useAuth();
  const [{ data, fetching, error }, refetch] = useQuery({
    query: PromptDocument,
    variables: { id },
    pause: loading || !user,
  });
  const [activateResult, activate] = useMutation(ActivatePromptVersionDocument);
  const [createResult, createVersion] = useMutation(CreatePromptVersionDocument);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftSaved, setDraftSaved] = useState<number | null>(null);

  const prompt = data?.prompt;
  const versions = prompt?.versions ?? [];
  const active = activeVersionOf(versions);
  const selected = versions.find((v) => v.id === selectedId) ?? active ?? versions[0] ?? null;

  // Select the active version once the prompt has loaded.
  useEffect(() => {
    if (selectedId === null && active) setSelectedId(active.id);
  }, [active, selectedId]);

  async function onActivate(versionId: string) {
    const result = await activate({ versionId });
    if (!result.error) {
      setDraftSaved(null);
      refetch({ requestPolicy: "network-only" });
    }
  }

  async function onSave(values: { systemPrompt: string; notes: string | null }) {
    const result = await createVersion({ promptId: id, input: values });
    const created = result.data?.createPromptVersion;
    if (created) {
      setSelectedId(created.id);
      setDraftSaved(created.version);
      refetch({ requestPolicy: "network-only" });
    }
  }

  if (fetching && !data) return <LoadingState label="Loading prompt…" />;
  const queryError = firstGraphQLError(error);
  if (!prompt) return queryError ? <Alert tone="danger">{queryError}</Alert> : null;

  const agentNames = prompt.agents.map((agent) => agent.name);

  return (
    <div className="space-y-4">
      <PageHeader
        title={prompt.name}
        description={prompt.description ?? undefined}
        breadcrumb={[{ href: "/dashboard/prompts", label: "Prompts" }]}
        meta={
          <>
            <span className="font-mono text-xs text-ink-subtle">{prompt.key}</span>
            {active ? <Badge tone="success">{`Active v${active.version}`}</Badge> : null}
          </>
        }
      />
      {draftSaved !== null ? (
        <Alert tone="success">
          {`Saved v${draftSaved} as a draft. It is not live until you activate it — `}
          <Link href="/dashboard/evaluations" className="underline">run an evaluation</Link>
          {" first to compare it with the active version."}
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <Card className="self-start">
          <CardHeader title="Versions" description="Newest first." />
          <VersionList versions={versions} selectedId={selected?.id ?? null} onSelect={setSelectedId} />
        </Card>
        {selected ? (
          <VersionView
            version={selected}
            activeVersion={active?.version ?? null}
            agentNames={agentNames}
            activating={activateResult.fetching}
            error={firstGraphQLError(activateResult.error)}
            onActivate={onActivate}
          />
        ) : null}
      </div>

      {selected ? (
        <NewVersionForm
          key={selected.id}
          baseText={selected.systemPrompt}
          baseVersion={selected.version}
          submitting={createResult.fetching}
          error={firstGraphQLError(createResult.error)}
          onSave={onSave}
        />
      ) : null}

      <Card>
        <CardHeader title="Used by" description="Each agent here answers on the active version." />
        <CardBody>
          {prompt.agents.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No agents use this prompt yet. Choose it in an agent’s Prompt card.
            </p>
          ) : (
            <ul className="divide-y divide-line">
              {prompt.agents.map((agent) => (
                <li key={agent.id} className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0">
                  <Link href={`/dashboard/agents/${agent.id}`} className="text-sm text-ink hover:underline">
                    {agent.name}
                  </Link>
                  <Badge tone={agentStatusTone(agent.status)}>{agentStatusLabel(agent.status)}</Badge>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
```

Check `lib/agent-status.ts`'s exported names and signatures (the agent page imports `agentStatusLabel, agentStatusTone`), and match `refetch`'s urql signature to how `agents/[id]/page.tsx` calls `refetchAgent`.

- [ ] **Step 7: Run tests, typecheck, lint** — `npx vitest run src/components/prompts && npm run typecheck && npm run lint` → PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/components/prompts "apps/web/src/app/dashboard/prompts/[id]/page.tsx"
git commit -m "feat(web): prompt detail with version history, activation and drafts"
```

---

### Task 6: Agent page Prompt card

**Files:**
- Create: `apps/web/src/components/agents/AgentPromptCard.tsx`, `AgentPromptCard.test.tsx`
- Modify: `apps/web/src/app/dashboard/agents/[id]/page.tsx`

**Interfaces:**
- Consumes: `PromptsDocument`, `SetAgentPromptDocument`, `AgentDocument` (with `promptId`), `activeVersionOf` (Task 3).
- Produces: `type PromptOption = { id: string; name: string; activeVersion: number | null }`; `AgentPromptCard({ prompts, fetching, failed, currentPromptId, saving, error, saved, onSave }: { prompts: readonly PromptOption[]; fetching: boolean; failed: boolean; currentPromptId: string | null; saving: boolean; error: string | null; saved: boolean; onSave: (promptId: string | null) => void })`.

- [ ] **Step 1: Write failing tests**

```tsx
// apps/web/src/components/agents/AgentPromptCard.test.tsx
// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentPromptCard, type PromptOption } from "./AgentPromptCard";

const prompts: PromptOption[] = [{ id: "p1", name: "Sales", activeVersion: 3 }];

function setup(overrides: Partial<React.ComponentProps<typeof AgentPromptCard>> = {}) {
  const onSave = vi.fn();
  render(
    <AgentPromptCard prompts={prompts} fetching={false} failed={false} currentPromptId={null} saving={false} error={null} saved={false} onSave={onSave} {...overrides} />,
  );
  return onSave;
}

describe("AgentPromptCard", () => {
  it("offers the built-in default and the organization's prompts", () => {
    setup();
    expect(screen.getByRole("option", { name: "Built-in default" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Sales" })).toBeInTheDocument();
    expect(screen.getByText(/cannot be pinned in an evaluation/i)).toBeInTheDocument();
  });

  it("shows the linked prompt's active version and links to it", () => {
    setup({ currentPromptId: "p1" });
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("p1");
    expect(screen.getByText(/runs v3/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open prompt/i })).toHaveAttribute("href", "/dashboard/prompts/p1");
  });

  it("saves null for the default", () => {
    const onSave = setup({ currentPromptId: "p1" });
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "default" } });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    expect(onSave).toHaveBeenCalledWith(null);
  });

  it("saves the chosen prompt id", () => {
    const onSave = setup();
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "p1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    expect(onSave).toHaveBeenCalledWith("p1");
  });

  it("says when the prompt list is loading", () => {
    setup({ prompts: [], fetching: true });
    expect(screen.getByText(/loading prompts/i)).toBeInTheDocument();
  });

  it("points to the Prompts page when there are none", () => {
    setup({ prompts: [] });
    expect(screen.getByRole("link", { name: /create one/i })).toHaveAttribute("href", "/dashboard/prompts");
  });

  it("does not pass the default off as the saved value when the list failed", () => {
    setup({ prompts: [], failed: true, currentPromptId: "p1" });
    expect(screen.getByText(/could not load prompts/i)).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Built-in default" })).toBeNull();
    expect(screen.getByRole("button", { name: "Save prompt" })).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run to verify failure** — `npx vitest run src/components/agents/AgentPromptCard.test.tsx` → FAIL.

- [ ] **Step 3: Implement**

```tsx
// apps/web/src/components/agents/AgentPromptCard.tsx
"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Select } from "@/components/ui/Input";

export type PromptOption = { id: string; name: string; activeVersion: number | null };

/** The select's value for "no prompt" -- a real choice here, so it is not the
 * empty string DESIGN.md reserves for an unset placeholder. */
const DEFAULT_VALUE = "default";

export function AgentPromptCard({
  prompts,
  fetching,
  failed,
  currentPromptId,
  saving,
  error,
  saved,
  onSave,
}: {
  prompts: readonly PromptOption[];
  fetching: boolean;
  failed: boolean;
  currentPromptId: string | null;
  saving: boolean;
  error: string | null;
  saved: boolean;
  onSave: (promptId: string | null) => void;
}) {
  const [value, setValue] = useState(currentPromptId ?? DEFAULT_VALUE);
  useEffect(() => setValue(currentPromptId ?? DEFAULT_VALUE), [currentPromptId]);

  const chosen = prompts.find((prompt) => prompt.id === value) ?? null;

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    onSave(value === DEFAULT_VALUE ? null : value);
  }

  let control: React.ReactNode;
  if (fetching && prompts.length === 0) {
    control = <p className="text-sm text-ink-muted">Loading prompts…</p>;
  } else if (failed) {
    control = <p className="text-sm text-ink-muted">Could not load prompts. The agent keeps its current prompt.</p>;
  } else {
    control = (
      <Field label="System prompt">
        {(props) => (
          <Select {...props} value={value} onChange={(e) => setValue(e.target.value)} title={chosen?.name}>
            <option value={DEFAULT_VALUE}>Built-in default</option>
            {prompts.map((prompt) => (
              <option key={prompt.id} value={prompt.id}>
                {prompt.name}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }

  return (
    <form onSubmit={onSubmit}>
      <Card>
        <CardHeader title="Prompt" description="The versioned system prompt this agent answers on." />
        <CardBody className="space-y-3">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          {control}
          {!failed && !fetching && prompts.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No prompts yet — <Link href="/dashboard/prompts" className="underline">create one</Link>.
            </p>
          ) : null}
          {!failed && chosen ? (
            <p className="text-sm text-ink-muted">
              {chosen.activeVersion !== null ? `Runs v${chosen.activeVersion}. ` : null}
              <Link href={`/dashboard/prompts/${chosen.id}`} className="underline">Open prompt</Link>
            </p>
          ) : null}
          {!failed && value === DEFAULT_VALUE ? (
            <p className="text-xs text-ink-subtle">
              Answers on the default are not tied to a versioned prompt, so they cannot be pinned in an evaluation.
            </p>
          ) : null}
        </CardBody>
        <CardFooter>
          <Button type="submit" disabled={failed} loading={saving} loadingLabel="Saving…">
            Save prompt
          </Button>
          {saved ? (
            <span role="status" className="text-sm font-medium text-success">
              Saved
            </span>
          ) : null}
        </CardFooter>
      </Card>
    </form>
  );
}
```

The "Loading prompts…" and "No prompts" states only show when there is nothing else to show; in the loaded-with-prompts case the select renders.

- [ ] **Step 4: Mount on the agent page** — in `apps/web/src/app/dashboard/agents/[id]/page.tsx`:

Add imports `AgentPromptCard` from `@/components/agents/AgentPromptCard`, `PromptsDocument` and `SetAgentPromptDocument` from `@/graphql/generated`, `activeVersionOf` from `@/lib/prompts`. Beside the other queries:

```tsx
  const [promptsResult] = useQuery({ query: PromptsDocument, pause: loading || !user });
  const [setPromptResult, setAgentPrompt] = useMutation(SetAgentPromptDocument);
  const [promptSaved, setPromptSaved] = useState(false);

  async function onSavePrompt(promptId: string | null) {
    setPromptSaved(false);
    const result = await setAgentPrompt({ agentId: id, promptId });
    if (!result.error) setPromptSaved(true);
  }
```

(Declare `onSavePrompt` next to the page's other handlers, after `agent` is defined.) Between the Behaviour `</form>` and the Tools `<div>`:

```tsx
      <AgentPromptCard
        prompts={(promptsResult.data?.prompts ?? []).map((prompt) => ({
          id: prompt.id,
          name: prompt.name,
          activeVersion: activeVersionOf(prompt.versions)?.version ?? null,
        }))}
        fetching={promptsResult.fetching}
        failed={promptsResult.error !== undefined}
        currentPromptId={agent.promptId ?? null}
        saving={setPromptResult.fetching}
        error={firstGraphQLError(setPromptResult.error)}
        saved={promptSaved}
        onSave={onSavePrompt}
      />
```

- [ ] **Step 5: Run tests, typecheck, lint** — `npx vitest run src/components/agents && npm run typecheck && npm run lint` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/agents/AgentPromptCard.tsx apps/web/src/components/agents/AgentPromptCard.test.tsx "apps/web/src/app/dashboard/agents/[id]/page.tsx"
git commit -m "feat(web): choose an agent's prompt on its page"
```

---

### Task 7: Nav goes live, docs, full verification

**Files:**
- Modify: `apps/web/src/components/shell/nav.ts`, `nav.test.ts`
- Delete (if unused): `apps/web/src/components/PlaceholderPage.tsx` (and its test, if any)
- Modify: `docs/DESIGN.md`, `docs/PHASE-6.md`, `README.md`

- [ ] **Step 1: Update the nav test first.** Prompts is the last `soon` item, so the existing `expect(soon.length).toBeGreaterThan(0)` in `nav.test.ts` ("gives every not-yet-built section the phase it arrives in") must go — keep only the `every(... phase)` assertion. Add:

```ts
  it("has Prompts live", () => {
    const prompts = NAV_GROUPS.flatMap((group) => group.items).find((item) => item.href === "/dashboard/prompts");
    expect(prompts?.state).toBe("live");
  });
```

Run `npx vitest run src/components/shell/nav.test.ts` → FAIL on the new test.

- [ ] **Step 2: Flip the item** in `nav.ts`: `{ href: "/dashboard/prompts", label: "Prompts", icon: "prompt", state: "live" },`. Run the nav test → PASS.

- [ ] **Step 3: Remove the dead placeholder.** `grep -rn PlaceholderPage apps/web/src`. If only its own file/test match, delete them. Leave the `soon` state support in `nav.ts`/`Sidebar.tsx` — later sections will use it.

- [ ] **Step 4: Docs.**
  - `docs/DESIGN.md`: add a "Version history" section describing the pattern Task 5 built — a selectable version list beside a read-only selected-version card; activation confirmed inline in the card footer naming its impact (`activationImpact`), rollback worded as such (`activationLabel`); new versions saved as drafts from the selected version, the form remounted by `key` per base version.
  - `docs/PHASE-6.md` §9: change the "First follow-up" paragraph and table row to say it is delivered — `setAgentPrompt` and the agent page's Prompt card link agents to prompts, and the Prompts section drafts, activates and rolls back versions — pointing to `docs/superpowers/specs/2026-09-25-prompts-dashboard-design.md`.
  - `README.md`: in the section describing dashboard sections and in the prompts bullet, add the Prompts section and agent linking. Record the spec's "Out" list (create-form prompt choice, rename/delete, variables editing, version diff) as not delivered.

- [ ] **Step 5: Full verification.**
  - API: `cd apps/api && uv run pytest -v` (localhost overrides) → all PASS. `uv run ruff check . && uv run mypy app` if the repo's `make lint` runs them — use `make lint`.
  - Web: `cd apps/web && npm test && npm run typecheck && npm run lint` → all PASS.
  - Schema drift: `make schema && make web-codegen && git status` → no changes.

- [ ] **Step 6: Commit**

```bash
git add -A apps/web/src/components docs README.md
git commit -m "feat(web): Prompts section goes live; docs record agent-prompt linking"
```
