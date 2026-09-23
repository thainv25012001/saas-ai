// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { type RunRow, RunsTable } from "./RunsTable";

function run(overrides: Partial<RunRow> = {}): RunRow {
  return {
    id: "r1",
    status: "COMPLETED",
    caseCount: 4,
    completedCount: 4,
    provider: "openai",
    model: "gpt-4o-mini",
    judgeModel: null,
    agentName: "Sales bot",
    promptVersion: 3,
    createdAt: "2026-09-23T10:00:00Z",
    startedAt: "2026-09-23T10:00:05Z",
    passRate: 0.75,
    costUsd: "0.0412",
    ...overrides,
  };
}

describe("RunsTable", () => {
  it("says there are no runs yet", () => {
    render(<RunsTable runs={[]} />);
    expect(screen.getByText("No runs yet")).toBeInTheDocument();
  });

  it("shows status, pass rate, prompt version, model, cost and links the run", () => {
    render(<RunsTable runs={[run({ judgeModel: "claude-judge" })]} />);
    expect(screen.getByRole("link", { name: /Completed/ })).toHaveAttribute("href", "/dashboard/evaluations/runs/r1");
    expect(screen.getByText("75%")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(screen.getByText("gpt-4o-mini")).toBeInTheDocument();
    expect(screen.getByText("judge: claude-judge")).toBeInTheDocument();
    expect(screen.getByText(/0\.0412/)).toBeInTheDocument();
  });

  it("shows progress, not a pass rate, for a run still in flight", () => {
    render(<RunsTable runs={[run({ status: "RUNNING", completedCount: 1, passRate: null, costUsd: null })]} />);
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("1 of 4")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});
