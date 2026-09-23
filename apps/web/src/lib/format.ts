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
