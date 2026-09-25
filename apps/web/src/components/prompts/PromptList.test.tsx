// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PromptList, type PromptRow } from "./PromptList";

const row: PromptRow = { id: "p1", name: "Sales", key: "sales_system", activeVersion: 3, versionCount: 4, agentCount: 2 };

describe("PromptList", () => {
  it("links each prompt and shows key, active version, count and usage", () => {
    render(<PromptList prompts={[row]} />);
    expect(screen.getByRole("link", { name: "Sales" })).toHaveAttribute("href", "/dashboard/prompts/p1");
    expect(screen.getByText("sales_system")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(screen.getByText("4 versions")).toBeInTheDocument();
    expect(screen.getByText("2 agents")).toBeInTheDocument();
  });

  it("says a prompt nobody uses is not used", () => {
    render(<PromptList prompts={[{ ...row, agentCount: 0 }]} />);
    expect(screen.getByText("Not used")).toBeInTheDocument();
  });
});
