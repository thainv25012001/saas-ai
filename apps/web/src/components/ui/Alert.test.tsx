// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Alert } from "./Alert";

describe("Alert", () => {
  it("announces a danger alert assertively", () => {
    render(<Alert tone="danger">Unknown model.</Alert>);
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown model.");
  });

  it("announces success and info politely, as a status", () => {
    render(<Alert tone="success">Saved</Alert>);
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("announces warn politely, as a status distinct from danger", () => {
    // Added for `step_limit_reached` (Phase 4 Task 8): a real, named outcome
    // -- not a crash -- so it must not interrupt a screen reader the way
    // `danger` does, and it must not carry danger's own visual tone either.
    render(<Alert tone="warn">Reached its step limit.</Alert>);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Reached its step limit.");
    expect(status).not.toHaveClass("border-danger-line");
    expect(status).toHaveClass("border-warn-line");
  });

  it("renders its title above the message", () => {
    render(
      <Alert tone="danger" title="Could not save">
        The model is not available for this provider.
      </Alert>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not save");
    expect(alert).toHaveTextContent("The model is not available for this provider.");
  });
});
