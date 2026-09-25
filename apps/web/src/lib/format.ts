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

/** A product price as the API sends it -- a decimal *string*, exact, with an
 * optional ISO 4217 code -- in the viewer's locale.
 *
 * `null` for no price, so the caller decides what an empty cell says. An
 * unknown currency code makes `Intl.NumberFormat` throw; it then falls back
 * to the plain amount and code, exactly as they arrived, rather than hiding a
 * price behind an error. */
export function formatPrice(price: string | null, currency: string | null): string | null {
  if (price === null) return null;
  const amount = Number(price);
  if (Number.isNaN(amount)) return currency ? `${price} ${currency}` : price;
  if (!currency) return amount.toLocaleString(undefined, { minimumFractionDigits: 2 });
  try {
    return amount.toLocaleString(undefined, { style: "currency", currency });
  } catch {
    return `${price} ${currency}`;
  }
}

/** A USD cost as the API sends it -- a decimal *string* -- or `—` when it is
 * `null`, which the API means as "not priced" (an unpriced model), never as
 * zero. Up to four decimals: one eval turn often costs a fraction of a cent,
 * and `$0.00` would claim it was free. */
export function formatUsd(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const amount = Number(value);
  if (Number.isNaN(amount)) return value;
  return amount.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

/** Milliseconds as `840 ms` or `2.1 s`; `—` for no figure. */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

const RELATIVE_TIME_FORMAT = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

/** Largest unit first, each with its length in seconds. `minute` is the
 * smallest named unit -- anything shorter reads as "now" instead of "37
 * seconds ago", which is not a distinction worth asking someone to read. */
const RELATIVE_TIME_UNITS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 60 * 60],
  ["month", 30 * 24 * 60 * 60],
  ["week", 7 * 24 * 60 * 60],
  ["day", 24 * 60 * 60],
  ["hour", 60 * 60],
  ["minute", 60],
];

/** An ISO timestamp as "5 minutes ago" / "in 5 minutes", for the
 * conversations list's `last_message_at` column. `now` defaults to the real
 * clock but takes a fixed value in tests, so the result does not depend on
 * when the suite happens to run.
 *
 * Falls back to the raw string for an unparseable value, the same
 * "show what arrived" rule `formatTimestamp` already follows. */
export function formatRelativeTime(value: string, now: number = Date.now()): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  const diffSeconds = (date.getTime() - now) / 1000;
  const magnitude = Math.abs(diffSeconds);

  for (const [unit, secondsInUnit] of RELATIVE_TIME_UNITS) {
    if (magnitude >= secondsInUnit) {
      return RELATIVE_TIME_FORMAT.format(Math.round(diffSeconds / secondsInUnit), unit);
    }
  }
  // Under a minute either way reads as "now" rather than counting seconds.
  return magnitude < 30
    ? RELATIVE_TIME_FORMAT.format(0, "second")
    : RELATIVE_TIME_FORMAT.format(Math.round(diffSeconds), "second");
}
