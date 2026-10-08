import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { LoadingState } from "@/components/ui/Spinner";
import { formatPrice, formatTimestamp } from "@/lib/format";
import {
  bestProfitText,
  comparabilityReasonLabel,
  matchStatusLabel,
  profitCell,
  profitStatusLabel,
  rankLabel,
  reportCounts,
} from "@/lib/supplier-quotes";

export type ReportRow = {
  id: string;
  profitStatus: string;
  bestProfit: string | null;
  bestProfitCurrency: string | null;
  summaryJson: unknown;
  createdAt: string;
  supplierId: string | null;
  senderEmail: string | null;
  receivedAt: string | null;
};

export type QuoteRow = {
  id: string;
  supplierId: string;
  productId: string | null;
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
  attachmentId: string;
  worksheetName: string;
  sourceRowNumber: number;
  matchStatus: string;
  catalogProductName: string | null;
  catalogProductSku: string | null;
};

export type ReportDetail = {
  quotes: QuoteRow[];
  attachments?: { id: string; filename: string | null }[];
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
  canManage,
  confirmingQuoteId,
  onConfirmMatch,
}: {
  reports: ReportRow[];
  supplierNames: Record<string, string>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  detail: ReportDetail | null;
  detailLoading: boolean;
  detailError: string | null;
  canManage: boolean;
  confirmingQuoteId: string | null;
  onConfirmMatch: (quoteId: string, productId: string) => void;
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
          const best = bestProfitText(report);
          const supplier = report.supplierId ? supplierNames[report.supplierId] : undefined;
          const when = formatTimestamp(report.receivedAt ?? report.createdAt);
          return (
            <li key={report.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-ink">
                  <span className="font-medium">{supplier ?? "Unknown supplier"}</span>
                  {report.senderEmail ? (
                    <span className="text-ink-muted"> · {report.senderEmail}</span>
                  ) : null}
                </p>
                <p className="mt-0.5 text-xs text-ink-muted">
                  <span>{when}</span>
                  {" · "}
                  <span>{plural(counts.quotes, "quote", "quotes")}</span>
                  {counts.issues > 0 ? (
                    <>
                      {" · "}
                      <span>{plural(counts.issues, "row needs review", "rows need review")}</span>
                    </>
                  ) : null}
                  {best !== null ? (
                    <>
                      {" · best gross profit/unit "}
                      {best}
                    </>
                  ) : null}
                </p>
              </div>
              <Badge tone={status.tone}>{status.label}</Badge>
              <Button
                variant={selected ? "primary" : "secondary"}
                size="sm"
                aria-pressed={selected}
                aria-label={`View report from ${when}`}
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
            <QuoteTable
              detail={detail}
              supplierNames={supplierNames}
              canManage={canManage}
              confirmingQuoteId={confirmingQuoteId}
              onConfirmMatch={onConfirmMatch}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function QuoteTable({
  detail,
  supplierNames,
  canManage,
  confirmingQuoteId,
  onConfirmMatch,
}: {
  detail: ReportDetail;
  supplierNames: Record<string, string>;
  canManage: boolean;
  confirmingQuoteId: string | null;
  onConfirmMatch: (quoteId: string, productId: string) => void;
}) {
  const { quotes } = detail;
  if (quotes.length === 0) {
    return (
      <p className="px-5 py-4 text-sm text-ink-muted">
        No rows from this email could be read. Check the items that need review above.
      </p>
    );
  }
  const filenames = Object.fromEntries((detail.attachments ?? []).map((item) => [item.id, item.filename]));
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm" aria-label="Quotes in this report">
          <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
            <tr>
              <th scope="col" className={TH}>Product</th>
              <th scope="col" className={TH}>Supplier</th>
              <th scope="col" className={TH}>Unit price</th>
              <th scope="col" className={TH}>Gross profit / unit</th>
              <th scope="col" className={TH}>Rank</th>
              <th scope="col" className={TH}>Source</th>
              <th scope="col" className={TH}>Notes</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {quotes.map((quote) => {
              const profit = profitCell(quote);
              const price = formatPrice(quote.unitPrice, quote.currency);
              const match = matchStatusLabel(quote.matchStatus);
              const source = [
                filenames[quote.attachmentId] ?? "Attachment",
                quote.worksheetName,
                `row ${quote.sourceRowNumber}`,
              ].join(" · ");
              return (
                <tr key={quote.id}>
                  <td className="max-w-xs px-5 py-3">
                    <span className="block truncate font-medium text-ink">
                      {quote.productName ?? "Unnamed item"}
                    </span>
                    {quote.supplierSku ? (
                      <span className="block truncate font-mono text-xs text-ink-subtle">{quote.supplierSku}</span>
                    ) : null}
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <Badge tone={match.tone}>{match.label}</Badge>
                      {quote.matchStatus !== "unmatched" && quote.catalogProductName ? (
                        <span className="text-xs text-ink-muted">
                          {quote.catalogProductName}
                          {quote.catalogProductSku ? ` (${quote.catalogProductSku})` : null}
                        </span>
                      ) : null}
                      {canManage && quote.matchStatus === "suggested" && quote.productId ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          loading={confirmingQuoteId === quote.id}
                          loadingLabel="Confirming…"
                          onClick={() => onConfirmMatch(quote.id, quote.productId as string)}
                        >
                          Confirm match
                        </Button>
                      ) : null}
                    </div>
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
                  <td className="px-5 py-3 text-xs text-ink-muted">{source}</td>
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
      <p className="border-t border-line px-5 py-3 text-xs text-ink-subtle">
        Gross profit is the catalogue sale price minus the quoted unit price, before shipping, duties, taxes
        and selling fees. Only each supplier&apos;s latest quote for a product is ranked.
      </p>
    </div>
  );
}
