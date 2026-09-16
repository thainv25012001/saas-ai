import type { AgentStatus } from "@/graphql/generated";
import type { ProviderInfo } from "./providers";

/** Only the field the checklist reads, so the Agents query can change shape
 * without touching this module. `provider` used to be read here too, to tell
 * whether any agent had been moved off `fake`; see `real-provider` below for
 * why that question moved to the server. */
export type ChecklistAgent = { status: AgentStatus };

export type ChecklistStepId = "create-agent" | "real-provider" | "activate-agent" | "test-agent";

export type ChecklistStep = {
  id: ChecklistStepId;
  title: string;
  description: string;
  /** `null` when the dashboard cannot know. Rendered as an action, never a tick. */
  done: boolean | null;
  action: { href: string; label: string };
};

/** Answers offline with a canned reply, and needs no API key -- so it is
 * always reported as `configured` and can never satisfy the step below. */
const OFFLINE_PROVIDER = "fake";

export function deriveChecklist(
  agents: readonly ChecklistAgent[],
  providers: readonly ProviderInfo[],
): ChecklistStep[] {
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
        "Set OPENAI_API_KEY, ANTHROPIC_API_KEY or OPENROUTER_API_KEY on the API. Until one is set there is no provider to create an agent on.",
      // Asked of the server, not of the agents. This used to be "some agent is
      // not on `fake`", which made sense when every agent was created on `fake`
      // by default. Creating an agent now requires choosing a configured
      // provider, so that phrasing would tick the moment the first agent
      // existed -- claiming a step the user had not done. An empty list (the
      // query has not resolved) is correctly false rather than true.
      done: providers.some(
        (provider) => provider.id !== OFFLINE_PROVIDER && provider.configured,
      ),
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
