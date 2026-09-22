/**
 * Formatting helpers shared by the dashboard's tables.
 *
 * These live here rather than beside one table because two tables showing
 * the same kind of value in two different ways is a bug the user sees, and
 * a copy in each file is how that happens.
 */

/** An ISO timestamp as a readable local date and time.
 *
 * Falls back to the raw string rather than rendering "Invalid Date": the
 * value came from the API, so if it is unparseable the honest thing is to
 * show what arrived rather than a placeholder that hides it. */
export function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
