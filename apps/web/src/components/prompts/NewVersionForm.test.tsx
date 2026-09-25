// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NewVersionForm } from "./NewVersionForm";

function setup() {
  const onSave = vi.fn();
  render(<NewVersionForm baseText="base" baseVersion={2} submitting={false} error={null} onSave={onSave} />);
  return onSave;
}

describe("NewVersionForm", () => {
  it("starts from the base version and cannot save it unchanged", () => {
    setup();
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("base");
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
  });

  it("is disabled again when edited back to the base text", () => {
    setup();
    const text = screen.getByLabelText(/system prompt/i);
    fireEvent.change(text, { target: { value: "base changed" } });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeEnabled();
    fireEvent.change(text, { target: { value: "base" } });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
  });

  it("is disabled for blank text", () => {
    setup();
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
  });

  it("saves the text and notes", () => {
    const onSave = setup();
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: "new" } });
    fireEvent.change(screen.getByLabelText(/notes/i), { target: { value: "tighter rules" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(onSave).toHaveBeenCalledWith({ systemPrompt: "new", notes: "tighter rules" });
  });
});
