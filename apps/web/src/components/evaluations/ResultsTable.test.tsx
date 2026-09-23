// @vitest-environment happy-dom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { type ResultRow, ResultsTable } from "./ResultsTable";

const SCRIPT = "<script>alert('pwned')</script>";

function result(overrides: Partial<ResultRow> = {}): ResultRow {
  return {
    id: "r1",
    caseId: "c1",
    question: "What does the warranty cover?",
    answer: "Parts and labour for three years.",
    error: null,
    passed: true,
    citedDocumentIds: ["doc-1"],
    citedProductIds: [],
    latencyMs: 840,
    costUsd: "0.0021",
    scores: [
      { name: "required_phrases", score: 1, passed: true, status: "scored", detail: '{"missing":[]}' },
      {
        name: "judge",
        score: 1,
        passed: true,
        status: "scored",
        detail: '{"correctness":"correct","grounded":true,"rationale":"Matches the reference."}',
      },
    ],
    toolCalls: [{ name: "retrieve_knowledge", arguments: '{"query":"warranty"}', isError: false, excerpt: "Warranty: 3 years" }],
    ...overrides,
  };
}

describe("ResultsTable", () => {
  it("shows pass/fail, a badge per score, latency and cost", () => {
    render(<ResultsTable results={[result(), result({ id: "r2", passed: false, costUsd: null })]} />);
    expect(screen.getByText("Pass")).toBeInTheDocument();
    expect(screen.getByText("Fail")).toBeInTheDocument();
    expect(screen.getAllByText("Required phrases 100%")).toHaveLength(2);
    expect(screen.getAllByText("Judge 100%")).toHaveLength(2);
    expect(screen.getAllByText("840 ms")).toHaveLength(2);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("expands a row to the answer, rationale, tool calls and citations", () => {
    render(<ResultsTable results={[result()]} documentTitles={{ "doc-1": "Warranty.pdf" }} />);
    const toggle = screen.getByRole("button", { name: /What does the warranty cover/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Parts and labour for three years.")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Parts and labour for three years.")).toBeInTheDocument();
    expect(screen.getByText("Matches the reference.")).toBeInTheDocument();
    expect(screen.getByText("retrieve_knowledge")).toBeInTheDocument();
    expect(screen.getByText("query: warranty")).toBeInTheDocument();
    expect(screen.getByText("Warranty: 3 years")).toBeInTheDocument();
    expect(screen.getByText("Warranty.pdf")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.queryByText("Parts and labour for three years.")).toBeNull();
  });

  it("renders <script> and markdown payloads in every untrusted field literally", () => {
    const { container } = render(
      <ResultsTable
        results={[
          result({
            question: `${SCRIPT} question`,
            answer: `**bold** ${SCRIPT} answer`,
            error: `${SCRIPT} error`,
            scores: [
              {
                name: "judge",
                score: 0,
                passed: false,
                status: "scored",
                detail: JSON.stringify({ correctness: "incorrect", grounded: false, rationale: `**bold** ${SCRIPT}` }),
              },
            ],
            toolCalls: [
              { name: "create_lead", arguments: JSON.stringify({ name: SCRIPT }), isError: true, excerpt: `${SCRIPT} excerpt` },
            ],
          }),
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /question/ }));

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("strong")).toBeNull();
    expect(screen.getByText(`${SCRIPT} question`)).toBeInTheDocument();
    expect(screen.getByText(`**bold** ${SCRIPT} answer`)).toBeInTheDocument();
    expect(screen.getByText(`${SCRIPT} error`)).toBeInTheDocument();
    expect(screen.getByText(`**bold** ${SCRIPT}`)).toBeInTheDocument();
    expect(screen.getByText(`name: ${SCRIPT}`)).toBeInTheDocument();
    expect(screen.getByText(`${SCRIPT} excerpt`)).toBeInTheDocument();
  });

  it("marks a judge that could not score as an error, not a verdict", () => {
    render(
      <ResultsTable
        results={[
          result({
            passed: false,
            scores: [{ name: "judge", score: null, passed: false, status: "error", detail: '{"error":"ValidationError"}' }],
          }),
        ]}
      />,
    );
    expect(screen.getByText("Judge: error")).toBeInTheDocument();
  });

  it("labels each row against a compared run, counts them, and filters to regressions", () => {
    render(
      <ResultsTable
        results={[
          result({ id: "a", question: "Regressed one", passed: false }),
          result({ id: "b", question: "Improved one" }),
          result({ id: "c", question: "Same one" }),
          result({ id: "d", question: "Brand new one" }),
          result({ id: "e", question: "Errored one", passed: false, error: "provider unavailable" }),
        ]}
        comparison={{ a: "regressed", b: "improved", c: "unchanged", d: "new", e: "errored" }}
      />,
    );
    const counts = screen.getByRole("list", { name: "Compared with the other run" });
    expect(within(counts).getByText("Regressed: 1")).toBeInTheDocument();
    expect(within(counts).getByText("Improved: 1")).toBeInTheDocument();
    expect(within(counts).getByText("Errored: 1")).toBeInTheDocument();
    expect(within(counts).getByText("Unchanged: 1")).toBeInTheDocument();
    expect(within(counts).getByText("New: 1")).toBeInTheDocument();

    // The errored row is labelled as such, not as a regression.
    const erroredRow = screen.getByText("Errored one").closest("tr");
    expect(erroredRow).not.toBeNull();
    // Twice: the comparison badge beside Fail, and the scores column's own.
    expect(within(erroredRow as HTMLElement).getAllByText("Errored")).toHaveLength(2);
    expect(within(erroredRow as HTMLElement).queryByText("Regressed")).toBeNull();

    fireEvent.click(screen.getByLabelText("Show only regressions and errors"));
    expect(screen.getByText("Regressed one")).toBeInTheDocument();
    expect(screen.getByText("Errored one")).toBeInTheDocument();
    expect(screen.queryByText("Improved one")).toBeNull();
    expect(screen.queryByText("Brand new one")).toBeNull();
  });

  it("says when nothing regressed or errored under the filter", () => {
    render(<ResultsTable results={[result({ id: "a" })]} comparison={{ a: "unchanged" }} />);
    fireEvent.click(screen.getByLabelText("Show only regressions and errors"));
    expect(screen.getByText("No case regressed or errored.")).toBeInTheDocument();
  });
});
