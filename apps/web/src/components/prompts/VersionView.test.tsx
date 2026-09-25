// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { VersionRow } from "./VersionList";
import { VersionView } from "./VersionView";

const v1: VersionRow = { id: "v1", version: 1, systemPrompt: "old text", isActive: false, notes: null, createdAt: "2026-09-24T10:00:00Z" };

function setup(overrides: Partial<React.ComponentProps<typeof VersionView>> = {}) {
  const onActivate = vi.fn();
  render(
    <VersionView version={v1} activeVersion={3} agentNames={["Abe", "Zed"]} activating={false} error={null} onActivate={onActivate} {...overrides} />,
  );
  return onActivate;
}

describe("VersionView", () => {
  it("shows the text read-only", () => {
    setup();
    expect(screen.getByText("old text")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("calls activating an older version a rollback, and confirms naming the agents", () => {
    const onActivate = setup();
    fireEvent.click(screen.getByRole("button", { name: "Roll back to v1" }));
    expect(onActivate).not.toHaveBeenCalled();
    expect(screen.getByText("v1 becomes live for Abe and Zed on their next message.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onActivate).toHaveBeenCalledWith("v1");
  });

  it("can back out of the confirmation", () => {
    const onActivate = setup();
    fireEvent.click(screen.getByRole("button", { name: "Roll back to v1" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onActivate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Roll back to v1" })).toBeInTheDocument();
  });

  it("offers no activation for the active version", () => {
    setup({ version: { ...v1, isActive: true }, activeVersion: 1 });
    expect(screen.queryByRole("button", { name: /activate|roll back/i })).toBeNull();
    expect(screen.getByText(/live for every agent using this prompt/i)).toBeInTheDocument();
  });
});
