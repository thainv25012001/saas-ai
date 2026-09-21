// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentToolsCard, type AgentToolRow } from "./AgentToolsCard";

function tool(overrides: Partial<AgentToolRow> = {}): AgentToolRow {
  return {
    id: "t1",
    name: "retrieve_knowledge",
    description: "Search the knowledge base for grounding.",
    isEnabled: true,
    ...overrides,
  };
}

describe("AgentToolsCard", () => {
  it("says there are no tools rather than rendering an empty list silently", () => {
    render(<AgentToolsCard tools={[]} onToggle={vi.fn()} />);
    expect(screen.getByText(/no tools available yet/i)).toBeInTheDocument();
  });

  it("shows an enabled tool's name, description and badge, with a Disable action", () => {
    render(<AgentToolsCard tools={[tool()]} onToggle={vi.fn()} />);
    expect(screen.getByText("retrieve_knowledge")).toBeInTheDocument();
    expect(screen.getByText("Search the knowledge base for grounding.")).toBeInTheDocument();
    expect(screen.getByText("Enabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable" })).toBeInTheDocument();
  });

  it("shows a disabled tool (create_lead, off by default) with an Enable action", () => {
    render(
      <AgentToolsCard
        tools={[tool({ id: "t2", name: "create_lead", isEnabled: false })]}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable" })).toBeInTheDocument();
  });

  it("calls onToggle with the tool's id and the flipped state", () => {
    const onToggle = vi.fn();
    render(
      <AgentToolsCard
        tools={[tool({ id: "t2", name: "create_lead", isEnabled: false })]}
        onToggle={onToggle}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Enable" }));
    expect(onToggle).toHaveBeenCalledWith("t2", true);
  });

  it("shows a loading state on only the row currently mid-toggle", () => {
    render(
      <AgentToolsCard
        tools={[tool({ id: "t1" }), tool({ id: "t2", name: "create_lead", isEnabled: false })]}
        togglingId="t2"
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Disable" })).not.toHaveAttribute("aria-busy");
    expect(screen.getByText("Saving…")).toBeInTheDocument();
  });
});
