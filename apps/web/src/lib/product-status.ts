import type { BadgeTone } from "@/components/ui/Badge";
import type {
  ProductAvailability,
  ProductImportStatus,
  ProductSearchIndex,
} from "@/graphql/generated";

/**
 * Three lookups, one per enum a product page shows as a `Badge` -- the
 * one-lookup shape of `document-status.ts`, so the table, the import list and
 * the poll predicate read the same mapping instead of inlining their own.
 */

/** `IN_STOCK` is the healthy default; `PREORDER` is a neutral notice (info);
 * `OUT_OF_STOCK` is warn -- sellable again later, not broken -- and
 * `DISCONTINUED` is settled and needs no attention, so neutral. */
export function availabilityLabel(availability: ProductAvailability): string {
  switch (availability) {
    case "IN_STOCK":
      return "In stock";
    case "OUT_OF_STOCK":
      return "Out of stock";
    case "PREORDER":
      return "Preorder";
    case "DISCONTINUED":
      return "Discontinued";
  }
}

export function availabilityTone(availability: ProductAvailability): BadgeTone {
  switch (availability) {
    case "IN_STOCK":
      return "success";
    case "OUT_OF_STOCK":
      return "warn";
    case "PREORDER":
      return "info";
    case "DISCONTINUED":
      return "neutral";
  }
}

/**
 * `null` for `INDEXED`: the normal state earns no badge, so the two that do
 * stand out. Both remaining states are `warn` -- configured but not fully
 * live, the tone's meaning for a DRAFT agent -- because the product is still
 * found by name and keyword; only matching by meaning is missing or out of
 * date.
 */
export function searchIndexLabel(state: ProductSearchIndex): string | null {
  switch (state) {
    case "INDEXED":
      return null;
    case "NOT_INDEXED":
      return "Not yet searchable by meaning";
    case "STALE":
      return "Search index out of date";
  }
}

export function isTerminalImportStatus(status: ProductImportStatus): boolean {
  return status === "COMPLETED" || status === "FAILED";
}

/**
 * A completed import with failed rows is terminal but not good -- the same
 * distinction `documentStatusLabel` draws for a ready document with zero
 * chunks -- so it is `warn` with the count in the label, not a green
 * "Completed" beside rows that never landed.
 */
export function importStatusLabel(status: ProductImportStatus, failedCount: number): string {
  switch (status) {
    case "PENDING":
      return "Queued";
    case "PROCESSING":
      return "Importing";
    case "FAILED":
      return "Failed";
    case "COMPLETED":
      return failedCount > 0
        ? `Completed — ${failedCount} ${failedCount === 1 ? "row" : "rows"} failed`
        : "Completed";
  }
}

export function importStatusTone(status: ProductImportStatus, failedCount: number): BadgeTone {
  switch (status) {
    case "PENDING":
      return "neutral";
    case "PROCESSING":
      return "info";
    case "FAILED":
      return "danger";
    case "COMPLETED":
      return failedCount > 0 ? "warn" : "success";
  }
}

/** `shouldPollDocuments`' counterpart: poll while any import can still move,
 * and never from a hidden tab. */
export function shouldPollImports(
  imports: readonly { status: ProductImportStatus }[],
  tabHidden: boolean,
): boolean {
  if (tabHidden) return false;
  return imports.some((record) => !isTerminalImportStatus(record.status));
}
