# Prompts dashboard and agent–prompt linking — design

Date: 2026-09-25
Status: approved in conversation; awaiting written-spec review

## Why

`/dashboard/prompts` is a `PlaceholderPage`, and `docs/PHASE-6.md` §9 names its
first follow-up: **no API or UI links an agent to a prompt.** `CreateAgentInput`
and `UpdateAgentInput` carry no `promptId`, so versioned prompts — and the
"draft a version, evaluate it, activate it" loop Phase 6 was built for — are
reachable only for agents whose `prompt_id` was set by the seed or by hand in
the database. Every other agent runs on `DEFAULT_SALES_SYSTEM_PROMPT` and its
answers record no `prompt_version_id`.

The backend for prompts themselves already exists and is not redesigned here:
`prompts` / `prompt_versions` (single-active partial unique index),
`PromptService` (`create_prompt` creates and activates v1, `create_version`
creates an inactive draft, `activate_version` swaps the active row), and
GraphQL `prompts`, `prompt`, `Prompt.versions`, `createPrompt`,
`createPromptVersion`, `activatePromptVersion`. `ChatService` already renders
the active (or pinned) version and records its id.

## Outcome

From the dashboard alone, an operator can: create a prompt, attach it to an
agent, draft a new version, evaluate that draft (the Evaluations start-run form
already pins a version), then activate it — or roll back by activating an older
one — and see which agents each prompt is live for.

## Scope

In:

1. `setAgentPrompt` mutation (link / unlink).
2. `Prompt.agents` field and `defaultSystemPrompt` query.
3. `/dashboard/prompts` list + create.
4. `/dashboard/prompts/[id]` version history, activate (rollback), new draft
   version, used-by list.
5. A Prompt card on the agent detail page.
6. Nav item goes `live`; docs updated.

Out (recorded as not delivered):

- Choosing a prompt in the create-agent form (it is chosen on the detail page).
- Renaming or deleting a prompt.
- Editing a version's declared `variables` (the column exists; GraphQL does not
  expose it, and nothing asks for it yet).
- A text diff between versions.
- Rollback as "copy an old version into a new one" — rollback is activation.

## 1. API

### 1.1 `setAgentPrompt(agentId: UUID!, promptId: UUID): Agent!`

A dedicated mutation rather than a `promptId` field on `UpdateAgentInput`,
because `AgentService.update_agent` dumps with `exclude_none=True`: `null`
already means "unchanged" there, so unlinking would need a second flag. Here
`promptId: null` means unlink, unambiguously.

`AgentService.set_prompt(agent_id, prompt_id | None)`:

- Loads the agent via `get_agent` (`NotFoundError` cross-tenant).
- When `prompt_id` is not `None`, resolves it through
  `PromptService.get_prompt`, which carries the explicit `organization_id`
  predicate. **This check is load-bearing:** `agents.prompt_id` is a plain
  foreign key, and a Postgres FK check is not subject to RLS, so without it an
  agent could be pointed at another organization's prompt id — and
  `ChatService._resolve_system_prompt` would then fail its `active_version`
  lookup (RLS hides the row), breaking the agent rather than leaking text, but
  still a cross-tenant write. A foreign or unknown id is `NotFoundError`
  ("prompt not found"), indistinguishable from each other, as elsewhere.
- Sets `agent.prompt_id`, flushes, refreshes (the same `updated_at` reason as
  `update_agent`), returns the agent.

No role restriction beyond membership, matching the existing agent and prompt
mutations.

### 1.2 `Prompt.agents: [Agent!]!`

The agents whose `prompt_id` is this prompt, ordered by name. Batched through a
new dataloader on `Context` (one query for `prompts { agents }`), backed by a
service method `AgentService.agents_by_prompt(prompt_ids)` that keys results
by prompt id with every requested id present, like
`PromptService.versions_by_prompt`. Returns the existing `Agent` GraphQL type
(it already has `id`, `name`, `status`).

An agent always runs its prompt's **active** version, so "which agents use
which version" is: these agents, on the version marked active.

### 1.3 `defaultSystemPrompt: String!`

Returns `DEFAULT_SALES_SYSTEM_PROMPT`, unrendered (placeholders intact).
Authenticated like every other query. The create form pre-fills from it, so a
first prompt starts from the text agents actually run today rather than a
blank box, and keeps `{{company_name}}` / `{{agent_name}}`.

## 2. `/dashboard/prompts`

- `PageHeader`: "Prompts", description "The versioned system prompts behind
  your agents."
- A list card: one row per prompt — name (links to detail), `key` in
  `font-mono text-xs`, active version (`v3`), version count, and agents using
  it ("2 agents" / "Not used"). Query: `prompts { id name key description
  versions { id version isActive } agents { id } }`.
- Empty state: explains that an agent with no prompt runs on the built-in
  default, and offers the create form.
- Create form (`components/prompts/CreatePromptForm.tsx`): name, key (client
  hint `^[a-z0-9_]+$`, derived from the name until edited), description
  (optional), system prompt `Textarea` pre-filled with `defaultSystemPrompt`.
  A help line names the variables filled in at answer time: `{{company_name}}`,
  `{{agent_name}}`. On success, navigate to the new prompt's page. A duplicate
  key surfaces the API's `ConflictError` message.

## 3. `/dashboard/prompts/[id]`

Query: `prompt(id) { id name key description versions { id version
systemPrompt isActive notes createdAt } agents { id name status } }`.

Layout, top to bottom:

- `PageHeader` with breadcrumb back to Prompts; meta shows key and active
  version.
- **Versions** (`components/prompts/VersionList.tsx`): newest first; each row
  shows `v{n}`, an `Active` success badge on the active one, notes (or "No
  notes"), and date. Selecting a row selects that version; the active version
  is selected on load.
- **Selected version** (`components/prompts/VersionView.tsx`): the text,
  read-only, in a `font-mono` pre-wrapped block. For a non-active version, an
  **Activate v{n}** button. Activation asks for confirmation inline (in the
  card footer, per DESIGN.md): "v{n} becomes live for {agent names} on their
  next message" — or "No agents use this prompt yet" — then calls
  `activatePromptVersion`. Activating an older version is the rollback; the
  wording says so for a version older than the active one ("Roll back to
  v{n}").
- **New version** (`components/prompts/NewVersionForm.tsx`): a `Textarea`
  pre-filled from the selected version's text, a notes field, and "Save draft".
  Saving calls `createPromptVersion` and creates an **inactive** version — it
  is selected afterwards, with a line explaining it is not live until
  activated and can be evaluated first (link to `/dashboard/evaluations`).
  Save is disabled while the text is unchanged from the selected version or
  empty.
- **Used by**: the linked agents with status badges, each linking to
  `/dashboard/agents/{id}`; empty text points to the agent page's Prompt card.

After activate or save, the prompt query is refetched `network-only`.

## 4. Agent detail page — Prompt card

`components/agents/AgentPromptCard.tsx`, placed after the model/behaviour
cards:

- `Select` labelled "System prompt": first option "Built-in default", then the
  organization's prompts by name. Uses `ui/Select` and follows the Dropdowns
  rules (labels, not keys; a `title` for long names).
- Below it: for a linked prompt, "Runs v{n}" plus a link to the prompt's page;
  for the default, one line saying answers are not tied to a versioned prompt
  and so cannot be pinned in an evaluation.
- Save in `CardFooter` calls `setAgentPrompt`; success shows a success `Alert`,
  failure the first GraphQL error. Loading, empty ("No prompts yet — create
  one") and failed prompt-list states each get their own message.

The page's `AgentDocument` already selects `promptId`.

## 5. Shell and docs

- `nav.ts`: Prompts becomes `state: "live"` (drop `phase`); `nav.test.ts`
  updated.
- The placeholder page is replaced.
- `docs/DESIGN.md`: record any new pattern (the version list/selected-version
  split and the inline activation confirmation).
- `docs/PHASE-6.md` §9: mark the first follow-up delivered, with a pointer.
- `README.md`: mention the Prompts section and agent linking.

## 6. Errors

- Cross-tenant or unknown ids: `NotFoundError` from the service layer, shown as
  the first GraphQL error.
- Duplicate key: `ConflictError` from `create_prompt`.
- Concurrent version creation: existing `ConflictError` in `create_version`;
  the form shows it and the user retries.
- Activating the already-active version is harmless (the service swaps it to
  itself); the UI does not offer it.

## 7. Testing

API (`tests/integration`, connecting as the app role so RLS applies — see the
host-run test notes):

- `set_prompt` links, unlinks (`None`), and rejects another organization's
  prompt id with `NotFoundError`, leaving `prompt_id` unchanged.
- `set_prompt` on another organization's agent is `NotFoundError`.
- GraphQL `setAgentPrompt`, `prompts { agents }` (batched, correct per prompt,
  foreign agents absent), and `defaultSystemPrompt`.
- A chat turn after linking records the active version's id (end-to-end proof
  that linking reaches `ChatService`).

Web (vitest + Testing Library, beside each component):

- `CreatePromptForm`: pre-fills the default, derives the key, submits the
  payload, shows a conflict error.
- `VersionList` / `VersionView`: active badge, selection, activate and
  rollback wording, confirmation names the agents.
- `NewVersionForm`: pre-fills, disabled while unchanged, submits notes.
- `AgentPromptCard`: default option, lists prompts, sends `null` for default,
  the three list states.
- `nav.test.ts`: Prompts is live.
