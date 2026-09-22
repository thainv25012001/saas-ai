import type { BadgeTone } from "@/components/ui/Badge";
import type { LeadStatus } from "@/graphql/generated";

/**
 * A lifecycle column becomes a tone through one lookup -- see
 * `document-status.ts`'s own worked example. `NEW` is the row someone has
 * not looked at yet (info, "needs attention"); `QUALIFIED` gets the same
 * `warn` tone a DRAFT agent does, because it names an in-progress state, not
 * a settled one; `WON`/`LOST` are the two terminal outcomes.
 */
export function leadStatusLabel(status: LeadStatus): string {
  switch (status) {
    case "NEW":
      return "New";
    case "CONTACTED":
      return "Contacted";
    case "QUALIFIED":
      return "Qualified";
    case "WON":
      return "Won";
    case "LOST":
      return "Lost";
  }
}

export function leadStatusTone(status: LeadStatus): BadgeTone {
  switch (status) {
    case "NEW":
      return "info";
    case "CONTACTED":
      return "neutral";
    case "QUALIFIED":
      return "warn";
    case "WON":
      return "success";
    case "LOST":
      return "danger";
  }
}
