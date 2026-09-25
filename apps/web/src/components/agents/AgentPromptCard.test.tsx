// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentPromptCard, type PromptOption } from "./AgentPromptCard";

const prompts: PromptOption[] = [{ id: "p1", name: "Sales", activeVersion: 3 }];

function setup(overrides: Partial<React.ComponentProps<typeof AgentPromptCard>> = {}) {
  const onSave = vi.fn();
  render(
    <AgentPromptCard prompts={prompts} fetching={false} failed={false} currentPromptId={null} saving={false} error={null} saved={false} onSave={onSave} {...overrides} />,
  );
  return onSave;
}

describe("AgentPromptCard", () => {
  it("offers the built-in default and the organization's prompts", () => {
    setup();
    expect(screen.getByRole("option", { name: "Built-in default" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Sales" })).toBeInTheDocument();
    expect(screen.getByText(/cannot be pinned in an evaluation/i)).toBeInTheDocument();
  });

  it("shows the linked prompt's active version and links to it", () => {
    setup({ currentPromptId: "p1" });
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("p1");
    expect(screen.getByText(/runs v3/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open prompt/i })).toHaveAttribute("href", "/dashboard/prompts/p1");
  });

  it("saves null for the default", () => {
    const onSave = setup({ currentPromptId: "p1" });
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "default" } });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    expect(onSave).toHaveBeenCalledWith(null);
  });

  it("saves the chosen prompt id", () => {
    const onSave = setup();
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "p1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    expect(onSave).toHaveBeenCalledWith("p1");
  });

  it("says when the prompt list is loading", () => {
    setup({ prompts: [], fetching: true });
    expect(screen.getByText(/loading prompts/i)).toBeInTheDocument();
  });

  it("points to the Prompts page when there are none", () => {
    setup({ prompts: [] });
    expect(screen.getByRole("link", { name: /create one/i })).toHaveAttribute("href", "/dashboard/prompts");
  });

  it("keeps a linked prompt the list does not contain, rather than showing the default", () => {
    const onSave = setup({ currentPromptId: "p9" });
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("p9");
    expect(screen.getByRole("option", { name: "Current prompt (not in this list)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    expect(onSave).toHaveBeenCalledWith("p9");
  });

  it("does not pass the default off as the saved value when the list failed", () => {
    setup({ prompts: [], failed: true, currentPromptId: "p1" });
    expect(screen.getByText(/could not load prompts/i)).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Built-in default" })).toBeNull();
    expect(screen.getByRole("button", { name: "Save prompt" })).toBeDisabled();
  });
});
