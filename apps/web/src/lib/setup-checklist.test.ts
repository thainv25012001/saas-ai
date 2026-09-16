import { describe, expect, it } from "vitest";
import { checklistProgress, deriveChecklist, type ChecklistAgent } from "./setup-checklist";
import type { ProviderInfo } from "./providers";

const NO_KEYS: ProviderInfo[] = [
  { id: "fake", configured: true },
  { id: "openai", configured: false },
  { id: "anthropic", configured: false },
  { id: "openrouter", configured: false },
];

const OPENAI_KEYED: ProviderInfo[] = [
  { id: "fake", configured: true },
  { id: "openai", configured: true },
  { id: "anthropic", configured: false },
  { id: "openrouter", configured: false },
];

function stepById(
  agents: readonly ChecklistAgent[],
  providers: readonly ProviderInfo[],
  id: string,
) {
  const step = deriveChecklist(agents, providers).find((candidate) => candidate.id === id);
  if (!step) throw new Error(`no step ${id}`);
  return step;
}

describe("deriveChecklist", () => {
  it("has nothing done for a brand-new organization", () => {
    const steps = deriveChecklist([], NO_KEYS);
    expect(steps.map((step) => step.done)).toEqual([false, false, false, null]);
  });

  it("completes create-agent as soon as one agent exists", () => {
    expect(stepById([{ status: "DRAFT" }], NO_KEYS, "create-agent").done).toBe(true);
  });

  it("reads real-provider from the server's keys, not from the agents", () => {
    // It used to mean "some agent is not on `fake`". Creating an agent now
    // requires choosing a configured provider, so that was about to become
    // trivially true the moment anyone created their first agent -- a tick for
    // a step they had not done. What it is really asking is whether this
    // install has an API key at all.
    expect(stepById([], OPENAI_KEYED, "real-provider").done).toBe(true);
  });

  it("leaves real-provider undone when only the offline provider is available", () => {
    // `fake` is always `configured` -- it needs no key. It must not satisfy a
    // step whose entire point is that a real model can answer.
    expect(stepById([{ status: "ACTIVE" }], NO_KEYS, "real-provider").done).toBe(false);
  });

  it("completes activate-agent only for an ACTIVE agent", () => {
    expect(stepById([{ status: "DRAFT" }], OPENAI_KEYED, "activate-agent").done).toBe(false);
    expect(stepById([{ status: "ACTIVE" }], OPENAI_KEYED, "activate-agent").done).toBe(true);
  });

  it("leaves test-agent unknowable, because the data cannot tell", () => {
    // No query exposes whether a conversation ever happened. `null` renders as
    // an action, never as a satisfied checkmark.
    expect(stepById([{ status: "ACTIVE" }], OPENAI_KEYED, "test-agent").done).toBeNull();
  });

  it("treats a provider list that has not loaded as nothing configured", () => {
    // The dashboard renders before the query resolves. An empty list must not
    // read as "a key is set".
    expect(stepById([], [], "real-provider").done).toBe(false);
  });
});

describe("checklistProgress", () => {
  it("counts only the steps whose completion can be known", () => {
    expect(checklistProgress(deriveChecklist([], NO_KEYS))).toEqual({ done: 0, total: 3 });
  });

  it("counts the completed knowable steps", () => {
    const steps = deriveChecklist([{ status: "ACTIVE" }], OPENAI_KEYED);
    expect(checklistProgress(steps)).toEqual({ done: 3, total: 3 });
  });
});
