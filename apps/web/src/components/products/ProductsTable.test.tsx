// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProductsTable, type ProductRow } from "./ProductsTable";

function product(overrides: Partial<ProductRow> = {}): ProductRow {
  return {
    id: "p1",
    externalId: "CAM-001",
    name: "Camry Hybrid LE",
    description: "A fuel-efficient family sedan.",
    category: "sedan",
    price: "32999.00",
    currency: "USD",
    attributes: [
      { key: "seats", value: "5" },
      { key: "fuel", value: "hybrid" },
    ],
    availability: "IN_STOCK",
    stockQuantity: 4,
    isActive: true,
    searchIndex: "INDEXED",
    updatedAt: "2026-09-20T10:30:00Z",
    ...overrides,
  };
}

describe("ProductsTable", () => {
  it("shows when each product was last updated", () => {
    render(<ProductsTable products={[product()]} />);
    expect(screen.getByRole("columnheader", { name: "Last updated" })).toBeInTheDocument();
    expect(
      screen.getByText(new Date("2026-09-20T10:30:00Z").toLocaleString()),
    ).toBeInTheDocument();
  });

  it("says nothing has been imported yet when the catalogue is empty", () => {
    render(<ProductsTable products={[]} />);
    expect(screen.getByText("No products yet")).toBeInTheDocument();
  });

  it("says nothing matches, not that nothing exists, when a filter is narrowing the list", () => {
    render(<ProductsTable products={[]} filtered />);
    expect(screen.getByText("No products match")).toBeInTheDocument();
    expect(screen.queryByText("No products yet")).not.toBeInTheDocument();
  });

  it("renders a product's price, availability, category and attributes", () => {
    render(<ProductsTable products={[product()]} />);
    expect(screen.getByText("Camry Hybrid LE")).toBeInTheDocument();
    expect(screen.getByText("CAM-001")).toBeInTheDocument();
    expect(screen.getByText("sedan")).toBeInTheDocument();
    expect(screen.getByText(/32,999\.00/)).toBeInTheDocument();
    expect(screen.getByText("In stock")).toBeInTheDocument();
    expect(screen.getByText("4 in stock")).toBeInTheDocument();
    expect(screen.getByText("seats")).toBeInTheDocument();
    expect(screen.getByText("hybrid")).toBeInTheDocument();
  });

  it("says a product has no price rather than leaving the cell blank", () => {
    render(<ProductsTable products={[product({ price: null, currency: null })]} />);
    expect(screen.getByText("No price")).toBeInTheDocument();
  });

  it("marks an inactive product", () => {
    render(<ProductsTable products={[product({ isActive: false })]} />);
    expect(screen.getByText("Inactive")).toBeInTheDocument();
  });

  it("flags a product the semantic search cannot match yet, and explains it once", () => {
    render(
      <ProductsTable
        products={[
          product({ id: "a", searchIndex: "NOT_INDEXED" }),
          product({ id: "b", searchIndex: "STALE" }),
        ]}
      />,
    );
    expect(screen.getByText("Not yet searchable by meaning")).toBeInTheDocument();
    expect(screen.getByText("Search index out of date")).toBeInTheDocument();
    expect(screen.getByText(/still finds every product by name, keyword and filters/)).toBeInTheDocument();
  });

  it("shows no index badge or explanation when every product is indexed", () => {
    render(<ProductsTable products={[product()]} />);
    expect(screen.queryByText(/searchable by meaning/)).not.toBeInTheDocument();
    expect(screen.queryByText(/still finds every product/)).not.toBeInTheDocument();
  });

  it("renders customer-supplied name, description and category as literal text, never as markup", () => {
    // A customer's CSV is rendered straight onto this page -- the fourth,
    // and most direct, source under docs/DESIGN.md's untrusted-text rule.
    const { container } = render(
      <ProductsTable
        products={[
          product({
            name: "<script>alert(1)</script>",
            description: "<script>alert('description')</script>",
            category: "<img src=x onerror=alert(1)>",
            externalId: "<b>SKU</b>",
          }),
        ]}
      />,
    );
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(screen.getByText("<script>alert('description')</script>")).toBeInTheDocument();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(screen.getByText("<b>SKU</b>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("renders attribute keys and values as literal text, never as markup", () => {
    // `attributes` is heterogeneous jsonb: both halves are customer-supplied,
    // so the key is as untrusted as the value.
    const { container } = render(
      <ProductsTable
        products={[
          product({
            attributes: [
              { key: "<script>alert('key')</script>", value: "<script>alert('value')</script>" },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByText("<script>alert('key')</script>")).toBeInTheDocument();
    expect(screen.getByText("<script>alert('value')</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });
});
