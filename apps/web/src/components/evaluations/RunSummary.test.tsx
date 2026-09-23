// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type RunSummaryData, RunSummary, type RunSummaryRun } from "./RunSummary";

const SUMMARY: RunSummaryData = {
  passed: 17,
  failed: 3,
  errored: 1,
  passRate: 0.85,
  costUsd: "0.0412",
  meanLatencyMs: 2140,
  scorers: [{ name: "tool_selection", mean: 0.95, passed: 19, applicable: 20 }],
};

function run(overrides: Partial<RunSummaryRun> = {}): RunSummaryRun {
  return { status: "COMPLETED", caseCount: 20, completedCount: 20, error: null, summary: SUMMARY, ...overrides };
}

describe("RunSummary", () => {
  it("shows the pass rate, counts, cost, latency and per-scorer means", () => {
    render(<RunSummary run={run()} />);
    expect(screen.getByText("85%")).toBeInTheDocument();
    expect(screen.getByText("17 / 3")).toBeInTheDocument();
    expect(screen.getByText(/1 errored/)).toBeInTheDocument();
    expect(screen.getByText(/0\.0412/)).toBeInTheDocument();
    expect(screen.getByText("2.1 s")).toBeInTheDocument();
    expect(screen.getByText("Tool selection")).toBeInTheDocument();
    expect(screen.getByText("95%")).toBeInTheDocument();
    expect(screen.getByText("19 of 20")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
  });

  it("shows — for a null cost, and says why", () => {
    render(<RunSummary run={run({ summary: { ...SUMMARY, costUsd: null } })} />);
    const tile = screen.getByText("Cost").parentElement as HTMLElement;
    expect(tile).toHaveTextContent("—");
    expect(tile).toHaveTextContent("A model in this run has no price.");
  });

  it("shows progress and a cancel button while running", () => {
    const onCancel = vi.fn();
    render(<RunSummary run={run({ status: "RUNNING", completedCount: 5, summary: null })} onCancel={onCancel} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "5");
    expect(bar).toHaveAttribute("aria-valuemax", "20");
    expect(screen.getByText("5 of 20 cases")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(onCancel).toHaveBeenCalled();
  });

  it("shows a failed run's reason as text", () => {
    const { container } = render(
      <RunSummary run={run({ status: "FAILED", summary: null, error: "<script>x</script>" })} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("<script>x</script>")).toBeInTheDocument();
  });
});
