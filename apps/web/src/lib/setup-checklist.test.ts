import { describe, expect, it } from "vitest";
import { checklistProgress, deriveChecklist, type ChecklistAgent } from "./setup-checklist";

function stepById(agents: readonly ChecklistAgent[], id: string) {
  const step = deriveChecklist(agents).find((candidate) => candidate.id === id);
  if (!step) throw new Error(`no step ${id}`);
  return step;
}

describe("deriveChecklist", () => {
  it("has nothing done for a brand-new organization", () => {
    const steps = deriveChecklist([]);
    expect(steps.map((step) => step.done)).toEqual([false, false, false, null]);
  });

  it("completes create-agent as soon as one agent exists", () => {
    expect(stepById([{ status: "DRAFT", provider: "fake" }], "create-agent").done).toBe(true);
  });

  it("does not count the fake provider as a real one", () => {
    // DEFAULT_LLM_PROVIDER is `fake`, so this is every fresh install: an
    // assistant that only pretends to answer. The checklist has to say so.
    expect(stepById([{ status: "ACTIVE", provider: "fake" }], "real-provider").done).toBe(false);
  });

  it("completes real-provider when any agent uses a live provider", () => {
    const agents: ChecklistAgent[] = [
      { status: "DRAFT", provider: "fake" },
      { status: "DRAFT", provider: "anthropic" },
    ];
    expect(stepById(agents, "real-provider").done).toBe(true);
  });

  it("completes activate-agent only for an ACTIVE agent", () => {
    expect(stepById([{ status: "DRAFT", provider: "openai" }], "activate-agent").done).toBe(false);
    expect(stepById([{ status: "ACTIVE", provider: "openai" }], "activate-agent").done).toBe(true);
  });

  it("leaves test-agent unknowable, because the data cannot tell", () => {
    // No query exposes whether a conversation ever happened. `null` renders as
    // an action, never as a satisfied checkmark.
    expect(stepById([{ status: "ACTIVE", provider: "openai" }], "test-agent").done).toBeNull();
  });
});

describe("checklistProgress", () => {
  it("counts only the steps whose completion can be known", () => {
    expect(checklistProgress(deriveChecklist([]))).toEqual({ done: 0, total: 3 });
  });

  it("counts the completed knowable steps", () => {
    const steps = deriveChecklist([{ status: "ACTIVE", provider: "openai" }]);
    expect(checklistProgress(steps)).toEqual({ done: 3, total: 3 });
  });
});
