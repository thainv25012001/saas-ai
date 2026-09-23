// @vitest-environment happy-dom
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProductImports, type ProductImportRow } from "./ProductImports";

function record(overrides: Partial<ProductImportRow> = {}): ProductImportRow {
  return {
    id: "i1",
    filename: "catalogue.csv",
    status: "COMPLETED",
    totalRows: 10,
    succeededCount: 10,
    failedCount: 0,
    error: null,
    createdAt: "2026-09-23T10:00:00Z",
    errors: [],
    ...overrides,
  };
}

describe("ProductImports", () => {
  it("renders nothing before the first import", () => {
    const { container } = render(<ProductImports imports={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a clean import's file name, outcome and counts", () => {
    render(<ProductImports imports={[record()]} />);
    expect(screen.getByText("catalogue.csv")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText(/10 of 10 rows imported/)).toBeInTheDocument();
  });

  it("names which rows failed and why, not just how many", () => {
    render(
      <ProductImports
        imports={[
          record({
            succeededCount: 8,
            failedCount: 2,
            errors: [
              { row: 3, externalId: "sku-3", message: "price: not a number" },
              { row: 7, externalId: null, message: "external_id: required" },
            ],
          }),
        ]}
      />,
    );
    // Terminal, but not a green "Completed" beside rows that never landed.
    expect(screen.getByText("Completed — 2 rows failed")).toBeInTheDocument();
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(within(rows[1]).getByText("3")).toBeInTheDocument();
    expect(within(rows[1]).getByText("sku-3")).toBeInTheDocument();
    expect(within(rows[1]).getByText("price: not a number")).toBeInTheDocument();
    expect(within(rows[2]).getByText("7")).toBeInTheDocument();
    expect(within(rows[2]).getByText("external_id: required")).toBeInTheDocument();
  });

  it("says when only the first page of failed rows is shown", () => {
    render(
      <ProductImports
        imports={[
          record({
            succeededCount: 0,
            failedCount: 120,
            errors: [{ row: 2, externalId: "a", message: "bad" }],
          }),
        ]}
      />,
    );
    expect(screen.getByText("Failed rows (first 1 of 120)")).toBeInTheDocument();
  });

  it("shows a whole-file failure's reason", () => {
    render(
      <ProductImports
        imports={[
          record({
            status: "FAILED",
            totalRows: null,
            succeededCount: 0,
            error: "CSV is missing required column(s): name",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("CSV is missing required column(s): name");
  });

  it("shows a completed import's warning, such as an embedding failure", () => {
    const warning =
      "All valid rows were imported, but 10 could not be embedded for search by meaning.";
    render(<ProductImports imports={[record({ error: warning })]} />);
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText(/10 of 10 rows imported/)).toBeInTheDocument();
    expect(screen.getByText(warning)).toBeInTheDocument();
  });

  it("shows an import still in flight without counts it does not have yet", () => {
    render(
      <ProductImports imports={[record({ status: "PROCESSING", totalRows: null, succeededCount: 0 })]} />,
    );
    expect(screen.getByText("Importing")).toBeInTheDocument();
    expect(screen.queryByText(/rows imported/)).not.toBeInTheDocument();
  });

  it("renders a file name, SKU and message from the upload as literal text, never as markup", () => {
    const { container } = render(
      <ProductImports
        imports={[
          record({
            filename: "<script>alert('file')</script>",
            failedCount: 1,
            errors: [
              {
                row: 2,
                externalId: "<script>alert('sku')</script>",
                message: "<script>alert('message')</script>",
              },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByText("<script>alert('file')</script>")).toBeInTheDocument();
    expect(screen.getByText("<script>alert('sku')</script>")).toBeInTheDocument();
    expect(screen.getByText("<script>alert('message')</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });
});
