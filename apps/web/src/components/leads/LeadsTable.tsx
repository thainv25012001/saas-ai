import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import type { LeadStatus } from "@/graphql/generated";
import { conversationLabel } from "@/lib/conversation-transcript";
import { formatTimestamp } from "@/lib/format";
import { leadStatusLabel, leadStatusTone } from "@/lib/lead-status";

export type LeadRow = {
  id: string;
  agentId: string;
  name: string | null;
  email: string | null;
  phone: string | null;
  interest: string | null;
  status: LeadStatus;
  /** The channel the capturing conversation happened on (`widget`,
   * `playground`, `api`) -- `null` for a lead captured before Phase 8 set
   * this (Task 4). Shown as a badge only when set, per the brief; a raw
   * string rather than an enum, so a label the API adds later still renders
   * instead of falling through a switch with no matching case. */
  source: string | null;
  createdAt: string;
  conversation: { id: string; title: string | null; preview: string | null } | null;
};

/** `source` is freeform on the server (Task 4), not a GraphQL enum -- today
 * it is always one of `widget`/`playground`/`api`, and capitalizing is
 * enough to read as a label rather than a wire value, without a lookup
 * table that would need updating for a value the API might add later. */
function sourceLabel(source: string): string {
  return source.length === 0 ? source : source[0].toUpperCase() + source.slice(1);
}

/** `name`/`email`/`phone`/`interest` are all independently nullable on the
 * model -- a chat rarely yields a complete contact card in one turn, so a
 * capture with only a phone number is still a real row, not something to
 * hide a blank for. */
function contactLine(lead: LeadRow): string {
  const parts = [lead.email, lead.phone].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" · ") : "No contact info given";
}

/**
 * Read-only this phase, per the brief: status, the conversation it came
 * from, and when. `name`/`interest`/the conversation's own title all
 * originate in a file or a visitor's typed text somewhere upstream (the
 * model's `create_lead` call, in turn the conversation itself) -- every one
 * of them is rendered here as a plain JSX text child only, matching
 * `DocumentsTable`'s and `ChatMessage`'s own untrusted-text rule.
 */
export function LeadsTable({ leads }: { leads: readonly LeadRow[] }) {
  if (leads.length === 0) {
    return (
      <EmptyState
        icon="lead"
        title="No leads yet"
        description="Once create_lead is enabled for an agent and a conversation captures one, it shows up here."
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
          <tr>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Name
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Contact
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Interest
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Status
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Conversation
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Source
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Captured
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {leads.map((lead) => (
            <tr key={lead.id}>
              <td className="max-w-xs px-5 py-3 font-medium text-ink">
                <span className="block truncate">{lead.name ?? "Unnamed"}</span>
              </td>
              <td className="max-w-xs px-5 py-3 text-ink-muted">
                <span className="block truncate">{contactLine(lead)}</span>
              </td>
              <td className="max-w-xs px-5 py-3 text-ink-muted">
                <span className="block truncate">{lead.interest ?? "—"}</span>
              </td>
              <td className="px-5 py-3">
                <Badge tone={leadStatusTone(lead.status)}>{leadStatusLabel(lead.status)}</Badge>
              </td>
              <td className="max-w-xs px-5 py-3 text-ink-muted">
                {lead.conversation ? (
                  <Link
                    href={`/dashboard/conversations?agent=${lead.agentId}&conversation=${lead.conversation.id}`}
                    className="block truncate underline-offset-2 hover:underline"
                  >
                    {conversationLabel(lead.conversation)}
                  </Link>
                ) : (
                  <span className="block truncate">Deleted conversation</span>
                )}
              </td>
              <td className="px-5 py-3">
                {lead.source !== null ? <Badge tone="neutral">{sourceLabel(lead.source)}</Badge> : null}
              </td>
              <td className="px-5 py-3 text-ink-muted">{formatTimestamp(lead.createdAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
