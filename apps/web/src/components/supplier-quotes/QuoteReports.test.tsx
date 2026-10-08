// @vitest-environment happy-dom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QuoteReports, type QuoteRow, type ReportRow } from "./QuoteReports";

function report(overrides: Partial<ReportRow> = {}): ReportRow {
  return {
    id: "r1",
    profitStatus: "missing_sale_price",
    bestProfit: null,
    bestProfitCurrency: null,
    summaryJson: { quote_count: 2, issue_count: 1 },
    createdAt: "2026-10-01T08:00:00Z",
    supplierId: "s1",
    senderEmail: "quotes@acme.example",
    receivedAt: "2026-10-01T07:00:00Z",
    ...overrides,
  };
}

function quote(overrides: Partial<QuoteRow> = {}): QuoteRow {
  return {
    id: "q1",
    supplierId: "s1",
    supplierSku: "SUP-1",
    productName: "Widget",
    unitPrice: "10.0000",
    currency: "USD",
    purchaseUnit: "each",
    profitStatus: "missing_sale_price",
    unitGrossProfit: null,
    purchasePriceRank: 1,
    rankingBasis: "purchase_price",
    comparabilityReasons: [],
    productId: null,
    attachmentId: "part:1",
    worksheetName: "Offers",
    sourceRowNumber: 7,
    matchStatus: "confirmed",
    catalogProductName: null,
    catalogProductSku: null,
    ...overrides,
  };
}

const base = {
  supplierNames: { s1: "Acme" },
  selectedId: null,
  onSelect: () => {},
  detail: null,
  detailLoading: false,
  detailError: null,
  canManage: true,
  confirmingQuoteId: null,
  onConfirmMatch: () => {},
};

describe("QuoteReports", () => {
  it("shows an empty state before any supplier email is analysed", () => {
    render(<QuoteReports {...base} reports={[]} />);
    expect(screen.getByText(/no supplier quotes yet/i)).toBeInTheDocument();
  });

  it("lists reports with their counts and selects one", () => {
    const onSelect = vi.fn();
    render(<QuoteReports {...base} reports={[report()]} onSelect={onSelect} />);
    expect(screen.getByText("2 quotes")).toBeInTheDocument();
    expect(screen.getByText("1 row needs review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /view report/i }));
    expect(onSelect).toHaveBeenCalledWith("r1");
  });

  it("says the sale price is missing and ranks by purchase price instead", () => {
    render(<QuoteReports {...base} reports={[report()]} selectedId="r1" detail={{ quotes: [quote()] }} />);
    const table = screen.getByRole("table", { name: /quotes in this report/i });
    expect(within(table).getByText("Missing sale price")).toBeInTheDocument();
    expect(within(table).getByText("#1 by purchase price · USD per each")).toBeInTheDocument();
    expect(within(table).getByText("Acme")).toBeInTheDocument();
  });

  it("shows profit only when it was computed", () => {
    render(
      <QuoteReports
        {...base}
        reports={[
          report({ profitStatus: "available", bestProfit: "2.5000", bestProfitCurrency: "USD" }),
        ]}
        selectedId="r1"
        detail={{
          quotes: [
            quote({ profitStatus: "available", unitGrossProfit: "2.5000", rankingBasis: "gross_profit" }),
          ],
        }}
      />,
    );
    const table = screen.getByRole("table", { name: /quotes in this report/i });
    expect(within(table).getByText(/2\.50/)).toBeInTheDocument();
    expect(within(table).queryByText("Missing sale price")).toBeNull();
  });

  it("labels an incomparable currency and does not rank it", () => {
    render(
      <QuoteReports
        {...base}
        reports={[report()]}
        selectedId="r1"
        detail={{
          quotes: [
            quote({
              currency: "EUR",
              profitStatus: "currency_mismatch",
              purchasePriceRank: null,
              rankingBasis: "none",
              comparabilityReasons: ["currency_mismatch"],
            }),
          ],
        }}
      />,
    );
    const table = screen.getByRole("table", { name: /quotes in this report/i });
    expect(within(table).getByText("Not ranked")).toBeInTheDocument();
    expect(within(table).getAllByText("Currency differs from catalogue").length).toBeGreaterThan(0);
  });

  it("shows loading and error states for the detail", () => {
    const { rerender } = render(
      <QuoteReports {...base} reports={[report()]} selectedId="r1" detailLoading />,
    );
    expect(screen.getByText(/loading report/i)).toBeInTheDocument();
    rerender(<QuoteReports {...base} reports={[report()]} selectedId="r1" detailError="Report not found" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Report not found");
  });

  it("renders supplier text as text, never markup", () => {
    const { container } = render(
      <QuoteReports
        {...base}
        reports={[report()]}
        selectedId="r1"
        detail={{ quotes: [quote({ productName: "<script>alert(1)</script>" })] }}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
  });

  it("offers no way to send a report anywhere", () => {
    render(<QuoteReports {...base} reports={[report()]} selectedId="r1" detail={{ quotes: [quote()] }} />);
    expect(screen.queryByRole("button", { name: /send|email|share|export|deliver/i })).toBeNull();
  });
});

describe("QuoteReports audit trail and matches", () => {
  it("says which email a report is about", () => {
    render(<QuoteReports {...base} reports={[report()]} />);
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText(/quotes@acme\.example/)).toBeInTheDocument();
  });

  it("traces each quote to its file, sheet and row, and labels profit as gross", () => {
    render(
      <QuoteReports
        {...base}
        reports={[report()]}
        selectedId="r1"
        detail={{ quotes: [quote()], attachments: [{ id: "part:1", filename: "prices.xlsx" }] }}
      />,
    );
    expect(screen.getByText("prices.xlsx · Offers · row 7")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /gross profit/i })).toBeInTheDocument();
    expect(screen.getByText(/before shipping, duties, taxes and selling fees/i)).toBeInTheDocument();
  });

  it("lets the owner accept a suggested catalogue match", () => {
    const onConfirmMatch = vi.fn();
    render(
      <QuoteReports
        {...base}
        reports={[report()]}
        selectedId="r1"
        onConfirmMatch={onConfirmMatch}
        detail={{
          quotes: [
            quote({
              matchStatus: "suggested",
              productId: "p-1",
              catalogProductName: "Catalogue Widget",
              catalogProductSku: "CAT-1",
              purchasePriceRank: null,
              rankingBasis: "none",
              profitStatus: "pending",
            }),
          ],
        }}
      />,
    );
    expect(screen.getByText(/Catalogue Widget/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /confirm match/i }));
    expect(onConfirmMatch).toHaveBeenCalledWith("q1", "p-1");
  });

  it("gives a member no way to confirm a match", () => {
    render(
      <QuoteReports
        {...base}
        canManage={false}
        reports={[report()]}
        selectedId="r1"
        detail={{
          quotes: [quote({ matchStatus: "suggested", productId: "p-1", catalogProductName: "W" })],
        }}
      />,
    );
    expect(screen.queryByRole("button", { name: /confirm match/i })).toBeNull();
  });
});
