import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { LoadingState } from "@/components/ui/Spinner";
import { formatPrice, formatTimestamp } from "@/lib/format";
import {
  comparabilityReasonLabel,
  profitCell,
  profitStatusLabel,
  rankLabel,
  reportCounts,
} from "@/lib/supplier-quotes";

export type ReportRow = {
  id: string;
  profitStatus: string;
  bestProfit: string | null;
  summaryJson: unknown;
  createdAt: string;
};

export type QuoteRow = {
  id: string;
  supplierId: string;
  supplierSku: string | null;
  productName: string | null;
  unitPrice: string | null;
  currency: string | null;
  purchaseUnit: string | null;
  profitStatus: string;
  unitGrossProfit: string | null;
  purchasePriceRank: number | null;
  rankingBasis: string;
  comparabilityReasons: string[];
};

const TH = "px-5 py-2.5 font-medium";

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * Report list plus the selected report's quotes. Every product name, SKU and
 * price here was read out of a supplier's spreadsheet, so all of it renders
 * as plain text children (docs/DESIGN.md, "Untrusted text").
 *
 * There is deliberately no send/export control: reports stay in the
 * dashboard until a delivery channel is designed (spec §8).
 */
export function QuoteReports({
  reports,
  supplierNames,
  selectedId,
  onSelect,
  detail,
  detailLoading,
  detailError,
}: {
  reports: ReportRow[];
  supplierNames: Record<string, string>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  detail: { quotes: QuoteRow[] } | null;
  detailLoading: boolean;
  detailError: string | null;
}) {
  if (reports.length === 0) {
    return (
      <EmptyState
        icon="product"
        title="No supplier quotes yet"
        description="When a known supplier emails an .xlsx quote to the connected mailbox, its comparison appears here."
      />
    );
  }

  return (
    <div>
      <ul className="divide-y divide-line">
        {reports.map((report) => {
          const counts = reportCounts(report.summaryJson);
          const status = profitStatusLabel(report.profitStatus);
          const selected = report.id === selectedId;
          return (
            <li key={report.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm text-ink">{formatTimestamp(report.createdAt)}</p>
                <p className="mt-0.5 text-xs text-ink-muted">
                  <span>{plural(counts.quotes, "quote", "quotes")}</span>
                  {counts.issues > 0 ? (
                    <>
                      {" · "}
                      <span>{plural(counts.issues, "row needs review", "rows need review")}</span>
                    </>
                  ) : null}
                  {report.bestProfit !== null ? (
                    <>
                      {" · best unit profit "}
                      {formatPrice(report.bestProfit, null)}
                    </>
                  ) : null}
                </p>
              </div>
              <Badge tone={status.tone}>{status.label}</Badge>
              <Button
                variant={selected ? "primary" : "secondary"}
                size="sm"
                aria-pressed={selected}
                aria-label={`View report from ${formatTimestamp(report.createdAt)}`}
                onClick={() => onSelect(report.id)}
              >
                View report
              </Button>
            </li>
          );
        })}
      </ul>

      {selectedId ? (
        <div className="border-t border-line">
          {detailLoading ? (
            <LoadingState label="Loading report…" />
          ) : detailError ? (
            <div className="p-5">
              <Alert tone="danger">{detailError}</Alert>
            </div>
          ) : detail ? (
            <QuoteTable quotes={detail.quotes} supplierNames={supplierNames} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function QuoteTable({
  quotes,
  supplierNames,
}: {
  quotes: QuoteRow[];
  supplierNames: Record<string, string>;
}) {
  if (quotes.length === 0) {
    return (
      <p className="px-5 py-4 text-sm text-ink-muted">
        No rows from this email could be read. Check the items that need review above.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm" aria-label="Quotes in this report">
        <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
          <tr>
            <th scope="col" className={TH}>Product</th>
            <th scope="col" className={TH}>Supplier</th>
            <th scope="col" className={TH}>Unit price</th>
            <th scope="col" className={TH}>Unit profit</th>
            <th scope="col" className={TH}>Rank</th>
            <th scope="col" className={TH}>Notes</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {quotes.map((quote) => {
            const profit = profitCell(quote);
            const price = formatPrice(quote.unitPrice, quote.currency);
            return (
              <tr key={quote.id}>
                <td className="max-w-xs px-5 py-3">
                  <span className="block truncate font-medium text-ink">
                    {quote.productName ?? "Unnamed item"}
                  </span>
                  {quote.supplierSku ? (
                    <span className="block truncate font-mono text-xs text-ink-subtle">{quote.supplierSku}</span>
                  ) : null}
                </td>
                <td className="px-5 py-3 text-ink-muted">{supplierNames[quote.supplierId] ?? "Unknown supplier"}</td>
                <td className="px-5 py-3 text-ink">
                  {price ?? "—"}
                  {quote.purchaseUnit ? (
                    <span className="text-xs text-ink-subtle"> / {quote.purchaseUnit}</span>
                  ) : null}
                </td>
                <td className="px-5 py-3">
                  {profit.kind === "profit" ? (
                    <span className="text-ink">{profit.text}</span>
                  ) : (
                    <Badge tone={profit.tone}>{profit.label}</Badge>
                  )}
                </td>
                <td className="px-5 py-3 text-ink-muted">{rankLabel(quote)}</td>
                <td className="px-5 py-3 text-xs text-ink-muted">
                  {quote.comparabilityReasons.length > 0
                    ? quote.comparabilityReasons.map(comparabilityReasonLabel).join("; ")
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
