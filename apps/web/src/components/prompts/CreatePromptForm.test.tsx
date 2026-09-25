// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreatePromptForm } from "./CreatePromptForm";

function setup(overrides: Partial<React.ComponentProps<typeof CreatePromptForm>> = {}) {
  const onCreate = vi.fn();
  render(
    <CreatePromptForm defaultPrompt="Default {{company_name}}" submitting={false} error={null} onCreate={onCreate} {...overrides} />,
  );
  return onCreate;
}

describe("CreatePromptForm", () => {
  it("starts the system prompt from the built-in default", () => {
    setup();
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue("Default {{company_name}}");
  });

  it("derives the key from the name until the key is edited", () => {
    setup();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Sales Prompt" } });
    expect(screen.getByLabelText(/^key/i)).toHaveValue("sales_prompt");
    fireEvent.change(screen.getByLabelText(/^key/i), { target: { value: "custom_key" } });
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Other" } });
    expect(screen.getByLabelText(/^key/i)).toHaveValue("custom_key");
  });

  it("submits the values, with an empty description as null", () => {
    const onCreate = setup();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Sales Prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "Create prompt" }));
    expect(onCreate).toHaveBeenCalledWith({
      name: "Sales Prompt",
      key: "sales_prompt",
      description: null,
      systemPrompt: "Default {{company_name}}",
    });
  });

  it("blocks a key the API would reject", () => {
    const onCreate = setup();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Sales" } });
    fireEvent.change(screen.getByLabelText(/^key/i), { target: { value: "Bad Key" } });
    fireEvent.click(screen.getByRole("button", { name: "Create prompt" }));
    expect(onCreate).not.toHaveBeenCalled();
    expect(screen.getByText(/lowercase letters, digits and underscores/i)).toBeInTheDocument();
  });

  it("shows the API's error", () => {
    setup({ error: "a prompt with key 'sales' already exists" });
    expect(screen.getByRole("alert")).toHaveTextContent("already exists");
  });
});
