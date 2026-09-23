// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type CaseRow, CasesTable, expectationChips } from "./CasesTable";

function row(overrides: Partial<CaseRow> = {}): CaseRow {
  return {
    id: "c1",
    question: "What does the warranty cover?",
    referenceAnswer: "Parts and labour for 3 years.",
    requiredPhrases: ["3 years", "parts"],
    expectedToolNames: ["retrieve_knowledge"],
    expectedDocumentIds: ["d1"],
    expectedProductIds: [],
    tags: ["warranty"],
    ...overrides,
  };
}

describe("expectationChips", () => {
  it("summarises each kind of expectation", () => {
    expect(expectationChips(row())).toEqual([
      "Reference answer",
      "2 phrases",
      "retrieve_knowledge",
      "1 document",
    ]);
  });
});

describe("CasesTable", () => {
  it("says a run needs a case when there are none", () => {
    render(<CasesTable cases={[]} onEdit={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText("No cases yet")).toBeInTheDocument();
  });

  it("shows the question, chips and tags, and wires edit and delete", () => {
    const onEdit = vi.fn();
    const onDelete = vi.fn();
    render(<CasesTable cases={[row()]} onEdit={onEdit} onDelete={onDelete} />);
    expect(screen.getByText("What does the warranty cover?")).toBeInTheDocument();
    expect(screen.getByText("2 phrases")).toBeInTheDocument();
    expect(screen.getByText("warranty")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Edit case/ }));
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ id: "c1" }));
    fireEvent.click(screen.getByRole("button", { name: /Delete case/ }));
    expect(onDelete).toHaveBeenCalledWith("c1");
  });

  it("renders a question as text, never markup", () => {
    const { container } = render(
      <CasesTable cases={[row({ question: "<script>alert(1)</script> **bold**" })]} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("strong")).toBeNull();
    expect(screen.getByText("<script>alert(1)</script> **bold**")).toBeInTheDocument();
  });
});
