// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VersionList, type VersionRow } from "./VersionList";

const versions: VersionRow[] = [
  { id: "v2", version: 2, systemPrompt: "two", isActive: false, notes: null, createdAt: "2026-09-25T10:00:00Z" },
  { id: "v1", version: 1, systemPrompt: "one", isActive: true, notes: "first cut", createdAt: "2026-09-24T10:00:00Z" },
];

describe("VersionList", () => {
  it("marks the active version and the selected one", () => {
    render(<VersionList versions={versions} selectedId="v2" onSelect={vi.fn()} />);
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /v2/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /v1/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("first cut")).toBeInTheDocument();
    expect(screen.getByText("No notes")).toBeInTheDocument();
  });

  it("selects a version on click", () => {
    const onSelect = vi.fn();
    render(<VersionList versions={versions} selectedId="v2" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /v1/ }));
    expect(onSelect).toHaveBeenCalledWith("v1");
  });
});
