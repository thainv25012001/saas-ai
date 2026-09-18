/**
 * The playground's per-turn model choice, against the agent's configured one.
 *
 * The choice is a pair, never a bare model id: a model only means anything
 * against the provider that serves it, so `gpt-4o` and `anthropic` is not a
 * thing anyone can be asking for. Both halves therefore travel together.
 *
 * The session-only part is enforced by omission -- nothing here writes to the
 * agent. The picker overrides the turn; the agent keeps its own model.
 */

export type ModelSelection = { provider: string; model: string };

/** Empty while the agent query has not resolved, which is not yet a choice. */
function isComplete(choice: ModelSelection): boolean {
  return choice.provider !== "" && choice.model !== "";
}

export function isOverridden(choice: ModelSelection, agent: ModelSelection): boolean {
  return (
    isComplete(choice) && (choice.provider !== agent.provider || choice.model !== agent.model)
  );
}

/**
 * The `provider`/`model` fields to merge into the chat request body -- empty
 * when the choice is the agent's own, so a playground nobody has touched puts
 * exactly the request on the wire it did before the picker existed.
 */
export function overrideFields(choice: ModelSelection, agent: ModelSelection): Partial<ModelSelection> {
  return isOverridden(choice, agent) ? { provider: choice.provider, model: choice.model } : {};
}
