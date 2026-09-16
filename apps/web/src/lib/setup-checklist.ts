import type { AgentStatus } from "@/graphql/generated";

/** Only the two fields the checklist reads, so the Agents query can change
 * shape without touching this module. */
export type ChecklistAgent = { status: AgentStatus; provider: string };

export type ChecklistStepId = "create-agent" | "real-provider" | "activate-agent" | "test-agent";

export type ChecklistStep = {
  id: ChecklistStepId;
  title: string;
  description: string;
  /** `null` when the dashboard cannot know. Rendered as an action, never a tick. */
  done: boolean | null;
  action: { href: string; label: string };
};

/** The API's `DEFAULT_LLM_PROVIDER`, which answers offline with a canned reply. */
const OFFLINE_PROVIDER = "fake";

export function deriveChecklist(agents: readonly ChecklistAgent[]): ChecklistStep[] {
  return [
    {
      id: "create-agent",
      title: "Create an agent",
      description: "An agent is one assistant, with its own model, prompt and behaviour.",
      done: agents.length > 0,
      action: { href: "/dashboard/agents", label: "Go to Agents" },
    },
    {
      id: "real-provider",
      title: "Connect a real model provider",
      description:
        "New agents start on the offline `fake` provider, which returns a canned reply and costs nothing. Switch to OpenAI, Anthropic or OpenRouter to get real answers.",
      done: agents.some((agent) => agent.provider !== OFFLINE_PROVIDER),
      action: { href: "/dashboard/agents", label: "Choose a provider" },
    },
    {
      id: "activate-agent",
      title: "Activate an agent",
      description: "A draft agent is configurable but not yet live for your customers.",
      done: agents.some((agent) => agent.status === "ACTIVE"),
      action: { href: "/dashboard/agents", label: "Review statuses" },
    },
    {
      id: "test-agent",
      // Nothing in the schema records whether a conversation has happened, so
      // this step is never claimed as done -- an honest action beats a tick
      // that might be a lie.
      title: "Send it a test message",
      description: "Watch a real answer stream back, with its tokens, latency and cost.",
      done: null,
      action: { href: "/dashboard/playground", label: "Open playground" },
    },
  ];
}

export function checklistProgress(steps: readonly ChecklistStep[]): { done: number; total: number } {
  const knowable = steps.filter((step) => step.done !== null);
  return { done: knowable.filter((step) => step.done === true).length, total: knowable.length };
}
