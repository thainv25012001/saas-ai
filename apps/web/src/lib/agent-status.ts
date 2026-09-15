import type { BadgeTone } from "@/components/ui/Badge";
import type { AgentStatus } from "@/graphql/generated";

/** DRAFT is a warning, not a failure: it means "configured but not live". */
export function agentStatusTone(status: AgentStatus): BadgeTone {
  switch (status) {
    case "ACTIVE":
      return "success";
    case "DRAFT":
      return "warn";
    case "DISABLED":
      return "neutral";
  }
}

export function agentStatusLabel(status: AgentStatus): string {
  switch (status) {
    case "ACTIVE":
      return "Active";
    case "DRAFT":
      return "Draft";
    case "DISABLED":
      return "Disabled";
  }
}
