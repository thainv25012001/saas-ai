// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DocumentsTable, type DocumentRow } from "./DocumentsTable";

function doc(overrides: Partial<DocumentRow> = {}): DocumentRow {
  return {
    id: "d1",
    title: "Pricing sheet.pdf",
    status: "READY",
    chunkCount: 12,
    error: null,
    createdAt: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("DocumentsTable", () => {
  it("shows an empty state with no documents", () => {
    render(<DocumentsTable documents={[]} onRetry={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText(/no documents yet/i)).toBeInTheDocument();
  });

  it("shows a ready document with chunks as plain success, no retry offered", () => {
    render(<DocumentsTable documents={[doc()]} onRetry={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("flags a ready document with zero chunks instead of showing plain success", () => {
    render(
      <DocumentsTable documents={[doc({ chunkCount: 0 })]} onRetry={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText(/no content extracted/i)).toBeInTheDocument();
  });

  it("shows a failed document's error and offers retry", () => {
    render(
      <DocumentsTable
        documents={[doc({ status: "FAILED", chunkCount: 0, error: "could not extract text" })]}
        onRetry={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("could not extract text");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("offers retry for a pending document too, per the API's own rule", () => {
    render(
      <DocumentsTable documents={[doc({ status: "PENDING", chunkCount: 0 })]} onRetry={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("does not offer retry for a document currently processing", () => {
    render(
      <DocumentsTable
        documents={[doc({ status: "PROCESSING", chunkCount: 0 })]}
        onRetry={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("calls onRetry with the document's id", () => {
    const onRetry = vi.fn();
    render(
      <DocumentsTable
        documents={[doc({ id: "d9", status: "FAILED", error: "x" })]}
        onRetry={onRetry}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledWith("d9");
  });

  it("calls onDelete with the document's id and title", () => {
    const onDelete = vi.fn();
    render(<DocumentsTable documents={[doc({ id: "d9" })]} onRetry={vi.fn()} onDelete={onDelete} />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    expect(onDelete).toHaveBeenCalledWith("d9", "Pricing sheet.pdf");
  });

  it("renders a title containing markup as literal text, not as an element", () => {
    // The title comes from a file the customer uploaded -- untrusted, same
    // as a citation's document_title. React's default escaping is what
    // this test actually exercises: a regression here would mean someone
    // reached for dangerouslySetInnerHTML.
    const { container } = render(
      <DocumentsTable documents={[doc({ title: "<b>evil</b>.pdf" })]} onRetry={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText("<b>evil</b>.pdf")).toBeInTheDocument();
    expect(container.querySelector("b")).toBeNull();
  });
});
