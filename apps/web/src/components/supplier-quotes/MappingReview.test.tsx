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
      worksheetName: "Quote",
      headerRow: 3,
      columns: [
        { field: "SUPPLIER_SKU", label: "Item" },
        { field: "UNIT_PRICE", label: "Price" },
        { field: "CURRENCY", label: "Cur" },
        { field: "UNIT", label: "UOM" },
      ],
      defaultCurrency: null,
      defaultPurchaseUnit: null,
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
