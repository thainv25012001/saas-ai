// @vitest-environment happy-dom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MappingReview, type MappingRow, type ReviewMessageRow } from "./MappingReview";

function mapping(overrides: Partial<MappingRow> = {}): MappingRow {
  return {
    id: "map-1",
    supplierId: "s1",
    worksheetName: "Quote",
    headerRow: 3,
    columns: [
      { field: "PRODUCT_NAME", label: "Item" },
      { field: "UNIT_PRICE", label: "Price" },
    ],
    columnLabels: ["Item", "Price", "Cur", "UOM"],
    sheetOptions: [],
    defaultCurrency: null,
    defaultPurchaseUnit: null,
    ...overrides,
  };
}

function message(overrides: Partial<ReviewMessageRow> = {}): ReviewMessageRow {
  return {
    id: "msg-1",
    senderEmail: "quotes@acme.example",
    receivedAt: "2026-10-01T08:00:00Z",
    status: "review_required",
    errorCode: null,
    attachmentCount: 1,
    ...overrides,
  };
}

const base = {
  messages: [],
  supplierNames: { s1: "Acme" },
  confirmingId: null,
  errors: {},
  canManage: true,
  onConfirm: () => {},
};

describe("MappingReview", () => {
  it("shows an empty state when nothing needs review", () => {
    render(<MappingReview {...base} mappings={[]} />);
    expect(screen.getByText(/nothing to review/i)).toBeInTheDocument();
  });

  it("previews the worksheet's header labels and the proposed columns", () => {
    render(<MappingReview {...base} mappings={[mapping()]} />);
    const form = screen.getByRole("form", { name: /acme/i });
    expect(within(form).getByText(/worksheet “Quote”, header row 3/i)).toBeInTheDocument();
    expect(within(form).getByText("Item, Price, Cur, UOM")).toBeInTheDocument();
    expect(within(form).getByLabelText("Product name")).toHaveValue("Item");
    expect(within(form).getByLabelText("Unit price")).toHaveValue("Price");
    expect(within(form).getByLabelText("Currency")).toHaveValue("");
  });

  it("keeps Confirm disabled until the server's required fields are covered", () => {
    render(<MappingReview {...base} mappings={[mapping()]} />);
    expect(screen.getByRole("button", { name: /confirm mapping/i })).toBeDisabled();
    expect(screen.getByText(/choose a currency column/i)).toBeInTheDocument();
  });

  it("lets the owner edit the mapping before confirming it", () => {
    const onConfirm = vi.fn();
    render(<MappingReview {...base} mappings={[mapping()]} onConfirm={onConfirm} />);
    fireEvent.change(screen.getByLabelText("Currency"), { target: { value: "Cur" } });
    fireEvent.change(screen.getByLabelText("Purchase unit"), { target: { value: "UOM" } });
    fireEvent.change(screen.getByLabelText("Product name"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Supplier SKU"), { target: { value: "Item" } });
    fireEvent.click(screen.getByRole("button", { name: /confirm mapping/i }));
    expect(onConfirm).toHaveBeenCalledWith(mapping(), {
      worksheet_name: "Quote",
      header_row: 3,
      columns: { supplier_sku: "Item", unit_price: "Price", currency: "Cur", unit: "UOM" },
      default_currency: null,
      default_purchase_unit: null,
    });
  });

  it("shows the server's refusal beside the mapping it is about", () => {
    render(
      <MappingReview {...base} mappings={[mapping()]} errors={{ "map-1": "mapping must use the reviewed worksheet headers" }} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("reviewed worksheet headers");
  });

  it("lists emails that need attention with a readable reason", () => {
    render(
      <MappingReview
        {...base}
        mappings={[]}
        messages={[message({ status: "failed", errorCode: "gmail_message_unavailable" })]}
      />,
    );
    expect(screen.getByText("quotes@acme.example")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("The email is no longer in Gmail.")).toBeInTheDocument();
  });
});

describe("MappingReview sheet choice and roles", () => {
  it("lets the owner move a proposal to another header row in the file", () => {
    const onConfirm = vi.fn();
    render(
      <MappingReview
        {...base}
        onConfirm={onConfirm}
        mappings={[
          mapping({
            worksheetName: "Cover",
            headerRow: 1,
            columns: [],
            columnLabels: ["Supplier", "ACME"],
            sheetOptions: [
              { worksheetName: "Cover", headerRow: 1, labels: ["Supplier", "ACME"] },
              { worksheetName: "Quote", headerRow: 3, labels: ["Item", "Price", "Cur", "UOM"] },
            ],
          }),
        ]}
      />,
    );
    const sheet = screen.getByLabelText(/worksheet and header row/i);
    fireEvent.change(sheet, { target: { value: (sheet as HTMLSelectElement).options[1].value } });
    expect(screen.getByText("Item, Price, Cur, UOM")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Product name"), { target: { value: "Item" } });
    fireEvent.change(screen.getByLabelText("Unit price"), { target: { value: "Price" } });
    fireEvent.change(screen.getByLabelText("Currency"), { target: { value: "Cur" } });
    fireEvent.change(screen.getByLabelText("Purchase unit"), { target: { value: "UOM" } });
    fireEvent.click(screen.getByRole("button", { name: /confirm mapping/i }));
    expect(onConfirm.mock.calls[0][1]).toMatchObject({ worksheet_name: "Quote", header_row: 3 });
  });

  it("shows a member the proposal without a way to confirm it", () => {
    render(<MappingReview {...base} canManage={false} mappings={[mapping()]} />);
    expect(screen.getByText("Item, Price, Cur, UOM")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm mapping/i })).toBeNull();
    expect(screen.getByText(/only the organization owner/i)).toBeInTheDocument();
  });
});
