/**
 * Supplier quote analysis: the REST calls GraphQL cannot make, and the
 * one-lookup labels the dashboard renders statuses through.
 *
 * Gmail's OAuth handshake and the reprocess queue are REST
 * (`apps/api/app/api/gmail.py`); suppliers, mappings and reports are read and
 * written over GraphQL. Profit and rank are computed by the API -- this file
 * only decides how an absent value is *described*, never what it is.
 */

import type { BadgeTone } from "@/components/ui/Badge";
import type { ConfirmSupplierMappingInput, SupplierQuoteField } from "@/graphql/generated";
import { type ApiError, fetchWithRefresh, parseErrorEnvelope } from "./api";
import type { DocumentAuth } from "./documents";
import { formatPrice } from "./format";

/** `snake_case` from the API as a sentence, for a value no lookup knows yet. */
function humanize(value: string): string {
  const text = value.replace(/_/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : value;
}

/* ------------------------------------------------------------- statuses -- */

const PROFIT_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  missing_sale_price: { label: "Missing sale price", tone: "warn" },
  stale_sale_price: { label: "Sale price older than 90 days", tone: "warn" },
  invalid_sale_price: { label: "Catalogue price is invalid", tone: "warn" },
  missing_sale_price_currency: { label: "Catalogue price has no currency", tone: "warn" },
  missing_catalog_unit: { label: "Catalogue has no sale unit", tone: "warn" },
  currency_mismatch: { label: "Currency differs from catalogue", tone: "warn" },
  unit_mismatch: { label: "Unit differs from catalogue", tone: "warn" },
  missing_quote_price: { label: "Quote has no price", tone: "warn" },
  invalid_quote_price: { label: "Quote price is invalid", tone: "warn" },
  quantity_tier_requires_order_quantity: { label: "Price depends on order quantity", tone: "warn" },
  incomplete: { label: "Not enough data to compare", tone: "warn" },
  unmatched: { label: "No catalogue product matched", tone: "info" },
  review_required: { label: "Needs review", tone: "info" },
  pending: { label: "Waiting for comparison", tone: "neutral" },
  available: { label: "Profit calculated", tone: "success" },
};

export function profitStatusLabel(status: string): { label: string; tone: BadgeTone } {
  return PROFIT_STATUS[status] ?? { label: humanize(status), tone: "neutral" };
}

export type ProfitCell =
  | { kind: "profit"; text: string }
  | { kind: "status"; label: string; tone: BadgeTone };

/** A profit figure only when the API computed one; otherwise *why* not. A
 * blank or zero cell would read as "no profit", which is a different claim. */
export function profitCell(quote: {
  unitGrossProfit: string | null;
  currency: string | null;
  profitStatus: string;
}): ProfitCell {
  if (quote.unitGrossProfit !== null) {
    return { kind: "profit", text: formatPrice(quote.unitGrossProfit, quote.currency) ?? quote.unitGrossProfit };
  }
  return { kind: "status", ...profitStatusLabel(quote.profitStatus) };
}

const RANKING_BASIS: Record<string, string> = {
  purchase_price: "by purchase price",
  gross_profit: "by gross profit",
};

/** The rank names its basis and its group: offers are ranked only against
 * others in the same currency and purchase unit, so "#1" alone would claim a
 * comparison the API refused to make. */
export function rankLabel(quote: {
  purchasePriceRank: number | null;
  rankingBasis: string;
  currency: string | null;
  purchaseUnit: string | null;
}): string {
  if (quote.purchasePriceRank === null || !(quote.rankingBasis in RANKING_BASIS)) return "Not ranked";
  const group = [quote.currency, quote.purchaseUnit ? `per ${quote.purchaseUnit}` : null]
    .filter(Boolean)
    .join(" ");
  const rank = `#${quote.purchasePriceRank} ${RANKING_BASIS[quote.rankingBasis]}`;
  return group ? `${rank} · ${group}` : rank;
}

const REASONS: Record<string, string> = {
  missing_quote_currency: "Quote has no currency",
  missing_purchase_unit: "Quote has no purchase unit",
  currency_mismatch: "Currency differs from catalogue",
  unit_mismatch: "Unit differs from catalogue",
  missing_catalog_unit: "Catalogue has no sale unit",
  missing_quote_price: "Quote has no price",
  invalid_quote_price: "Quote price is invalid",
  no_positive_quote_prices: "No positive prices to rank",
  quantity_tier_requires_common_order_quantity: "Price depends on order quantity",
  missing_sale_price: "Missing sale price",
  stale_sale_price: "Sale price older than 90 days",
};

export function comparabilityReasonLabel(reason: string): string {
  return REASONS[reason] ?? humanize(reason);
}

const MESSAGE_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  review_required: { label: "Needs review", tone: "info" },
  failed: { label: "Failed", tone: "danger" },
};

export function messageStatusLabel(status: string): { label: string; tone: BadgeTone } {
  return MESSAGE_STATUS[status] ?? { label: humanize(status), tone: "neutral" };
}

/** Error codes the worker records on a message, in words an owner can act on. */
const MESSAGE_ERRORS: Record<string, string> = {
  gmail_connection_unavailable: "Gmail is disconnected. Reconnect it to reprocess this email.",
  gmail_message_unavailable: "The email is no longer in Gmail.",
  gmail_refresh_failed: "Gmail access expired. Reconnect Gmail.",
};

export function messageErrorLabel(code: string): string {
  return MESSAGE_ERRORS[code] ?? humanize(code);
}

/** `summaryJson` is `unknown` on the wire; read only the two counts, and
 * treat anything else as zero rather than trusting its shape. */
export function reportCounts(summary: unknown): { quotes: number; issues: number } {
  const record = summary && typeof summary === "object" ? (summary as Record<string, unknown>) : {};
  const count = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : 0);
  return { quotes: count(record.quote_count), issues: count(record.issue_count) };
}

/* -------------------------------------------------------------- suppliers -- */

/** Sender addresses or domains typed as free text: one per line, or comma-
 * or space-separated. Lower-cased and de-duplicated, as the API stores them. */
export function parseAddressList(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[\s,;]+/)) {
    const value = part.trim().toLowerCase();
    if (value) seen.add(value);
  }
  return [...seen];
}

/* --------------------------------------------------------------- mappings -- */

/** Every field a column can be mapped to, in the order the form shows them. */
export const MAPPING_FIELDS: { field: SupplierQuoteField; label: string }[] = [
  { field: "PRODUCT_NAME", label: "Product name" },
  { field: "SUPPLIER_SKU", label: "Supplier SKU" },
  { field: "SPECIFICATION", label: "Specification" },
  { field: "UNIT_PRICE", label: "Unit price" },
  { field: "CURRENCY", label: "Currency" },
  { field: "UNIT", label: "Purchase unit" },
  { field: "MINIMUM_ORDER_QUANTITY", label: "Minimum order quantity" },
  { field: "QUANTITY_TIERS", label: "Quantity tiers" },
  { field: "LEAD_TIME", label: "Lead time" },
  { field: "VALID_UNTIL", label: "Valid until" },
];

export type MappingDraft = {
  /** Field -> chosen header label, `""` for not mapped. */
  fields: Record<SupplierQuoteField, string>;
  defaultCurrency: string;
  defaultPurchaseUnit: string;
};

export function emptyMappingDraft(proposal: {
  columns: { field: SupplierQuoteField; label: string }[];
  defaultCurrency: string | null;
  defaultPurchaseUnit: string | null;
}): MappingDraft {
  const fields = Object.fromEntries(MAPPING_FIELDS.map(({ field }) => [field, ""])) as Record<
    SupplierQuoteField,
    string
  >;
  for (const column of proposal.columns) fields[column.field] = column.label;
  return {
    fields,
    defaultCurrency: proposal.defaultCurrency ?? "",
    defaultPurchaseUnit: proposal.defaultPurchaseUnit ?? "",
  };
}

/** `WorkbookMapping`'s validators, mirrored with the same rules so Confirm is
 * disabled before the server would refuse it. The server's check still
 * counts. */
export function mappingProblems(draft: MappingDraft, labels: string[]): string[] {
  const { fields } = draft;
  const problems: string[] = [];
  if (!fields.UNIT_PRICE) problems.push("Choose the column that holds the unit price.");
  if (!fields.PRODUCT_NAME && !fields.SUPPLIER_SKU) {
    problems.push("Choose a product name or supplier SKU column.");
  }
  if (!fields.CURRENCY && !draft.defaultCurrency.trim()) {
    problems.push("Choose a currency column or enter a default currency.");
  }
  if (!fields.UNIT && !draft.defaultPurchaseUnit.trim()) {
    problems.push("Choose a unit column or enter a default purchase unit.");
  }
  const chosen = Object.values(fields).filter(Boolean);
  if (new Set(chosen).size !== chosen.length) {
    problems.push("Each column can be used for only one field.");
  }
  if (chosen.some((label) => !labels.includes(label))) {
    problems.push("A chosen column is not in this worksheet.");
  }
  return problems;
}

export function buildMappingInput(
  proposal: { worksheetName: string; headerRow: number },
  draft: MappingDraft,
): ConfirmSupplierMappingInput {
  return {
    worksheetName: proposal.worksheetName,
    headerRow: proposal.headerRow,
    columns: MAPPING_FIELDS.filter(({ field }) => draft.fields[field]).map(({ field }) => ({
      field,
      label: draft.fields[field],
    })),
    defaultCurrency: draft.defaultCurrency.trim() || null,
    defaultPurchaseUnit: draft.defaultPurchaseUnit.trim() || null,
  };
}

/* ------------------------------------------------------------------ Gmail -- */

const CALLBACK_ERRORS: Record<string, string> = {
  authorization_denied: "Gmail access was declined, so nothing was connected.",
  invalid_state: "That Gmail sign-in link expired. Start the connection again.",
  refresh_token_missing:
    "Google did not grant offline access. Remove this app from your Google account's permissions, then connect again.",
  provider_error: "Google could not complete the connection. Try again.",
  gmail_notifications_not_configured: "Gmail notifications are not configured on the server.",
  gmail_notifications_unavailable: "Gmail would not start notifications for this mailbox. Try again.",
};

/** The `?gmail=` / `?reason=` the OAuth callback redirects back with. */
export function gmailCallbackNotice(
  gmail: string | null,
  reason: string | null,
): { tone: "success" | "danger"; message: string } | null {
  if (gmail === "connected") {
    return {
      tone: "success",
      message: "Gmail is connected. New supplier emails will be analysed as they arrive.",
    };
  }
  if (gmail === "error") {
    return {
      tone: "danger",
      message: (reason && CALLBACK_ERRORS[reason]) ?? "Gmail could not be connected. Try again.",
    };
  }
  return null;
}

async function send(
  path: string,
  method: "POST" | "DELETE",
  auth: DocumentAuth,
): Promise<Response> {
  const response = await fetchWithRefresh(
    (token) =>
      fetch(`${auth.apiUrl}${path}`, { method, headers: { Authorization: `Bearer ${token}` } }),
    auth.accessToken,
    auth.onAccessToken,
  );
  if (!response.ok) throw await parseErrorEnvelope(response, "internal_error");
  return response;
}

/** Google's consent URL. The caller navigates to it; the route cannot
 * redirect because a navigation would not carry the Bearer token. */
export async function startGmailConnect(auth: DocumentAuth): Promise<string> {
  const response = await send("/api/v1/gmail/oauth/start", "POST", auth);
  const body = (await response.json()) as { authorization_url?: unknown };
  if (typeof body.authorization_url !== "string") {
    throw { code: "internal_error", message: "The server did not return a Gmail sign-in link." } satisfies ApiError;
  }
  return body.authorization_url;
}

export async function disconnectGmail(auth: DocumentAuth): Promise<void> {
  await send("/api/v1/gmail/connection", "DELETE", auth);
}

/** Queues the Gmail re-fetch for a message whose mapping was just confirmed.
 * Called only after `confirmSupplierMapping` has returned, so the worker
 * always reads a committed mapping. */
export async function reprocessSupplierMessage(messageId: string, auth: DocumentAuth): Promise<void> {
  await send(`/api/v1/gmail/supplier-messages/${encodeURIComponent(messageId)}/reprocess`, "POST", auth);
}
