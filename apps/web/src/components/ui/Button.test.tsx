// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button } from "./Button";

describe("Button", () => {
  it("shows the loading label and disables itself while loading", () => {
    render(
      <Button loading loadingLabel="Saving…">
        Save agent
      </Button>,
    );
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Saving…");
    expect(button).not.toHaveTextContent("Save agent");
  });

  it("shows its children and stays enabled when not loading", () => {
    render(<Button loadingLabel="Saving…">Save agent</Button>);
    const button = screen.getByRole("button");
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Save agent");
  });

  it("stays disabled when disabled is passed without loading", () => {
    render(<Button disabled>Send</Button>);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("keeps the caller's own classes alongside the variant classes", () => {
    render(
      <Button variant="danger" className="mt-3">
        Delete
      </Button>,
    );
    const button = screen.getByRole("button");
    expect(button.className).toContain("mt-3");
    expect(button.className).toContain("text-danger");
  });
});
