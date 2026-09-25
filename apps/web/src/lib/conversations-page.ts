/**
 * Pure helpers behind `/dashboard/conversations` (spec §7): which channel
 * filter and which agent the page opens on. Kept separate from the page's
 * own data fetching so the URL-and-list logic can be tested without a
 * GraphQL client -- the page component itself follows this app's usual
 * pattern of owning queries with no dedicated test file (see
 * `app/dashboard/leads/page.tsx`, `app/dashboard/playground/page.tsx`).
 */

import type { ConversationChannel } from "@/graphql/generated";

export const CHANNEL_FILTERS = ["WIDGET", "PLAYGROUND", "API", "ALL"] as const;
export type ChannelFilter = (typeof CHANNEL_FILTERS)[number];

const DEFAULT_CHANNEL_FILTER: ChannelFilter = "WIDGET";

export function channelFilterLabel(filter: ChannelFilter): string {
  switch (filter) {
    case "WIDGET":
      return "Widget";
    case "PLAYGROUND":
      return "Playground";
    case "API":
      return "API";
    case "ALL":
      return "All channels";
  }
}

/** What the `conversations` query's `channel` variable should be for a given
 * filter. `ALL` has no wire value of its own on `ConversationChannel` --
 * "every channel" is expressed by omitting the argument, which the query
 * already defaults to `null`. */
export function channelQueryValue(filter: ChannelFilter): ConversationChannel | undefined {
  return filter === "ALL" ? undefined : filter;
}

/** `?channel=` from the URL, defaulting to the widget's own traffic -- the
 * channel this page exists to read (spec §7). An unrecognized or missing
 * value is the same as not specifying one, never an error. */
export function parseChannelFilter(value: string | null): ChannelFilter {
  return (CHANNEL_FILTERS as readonly string[]).includes(value ?? "")
    ? (value as ChannelFilter)
    : DEFAULT_CHANNEL_FILTER;
}

/** Which agent the page opens on: `?agent=` (set by a link from the Leads
 * page) when it names one of the agents actually loaded, else the first
 * agent once the list resolves -- the same precedent the Leads and
 * Playground pages' own agent pickers already follow. `null` only when
 * there are no agents at all yet. */
export function pickDefaultAgentId(
  agents: readonly { id: string }[],
  fromQuery: string | null,
): string | null {
  if (fromQuery && agents.some((agent) => agent.id === fromQuery)) return fromQuery;
  return agents[0]?.id ?? null;
}
