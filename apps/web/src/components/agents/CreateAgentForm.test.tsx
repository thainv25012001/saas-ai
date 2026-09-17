// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreateAgentForm } from "./CreateAgentForm";
import type { ModelChoice } from "./ModelPicker";
import type { ProviderInfo } from "@/lib/providers";

const PROVIDERS: ProviderInfo[] = [
  { id: "fake", configured: true },
  { id: "openai", configured: true },
  { id: "anthropic", configured: false },
  { id: "openrouter", configured: true },
];

const MODELS: ModelChoice[] = [
  { id: "gpt-4o-mini", label: "GPT-4o mini", contextLength: 128000 },
  { id: "gpt-4o", label: "GPT-4o", contextLength: 128000 },
];

function renderForm(overrides: Partial<React.ComponentProps<typeof CreateAgentForm>> = {}) {
  const props = {
    providers: PROVIDERS,
    models: MODELS,
    modelsFetching: false,
    modelsFailed: false,
    submitting: false,
    error: null,
    onProviderChange: vi.fn(),
    onSubmit: vi.fn(),
    onCancel: vi.fn(),
    provider: "openai",
    ...overrides,
  };
  return { props, ...render(<CreateAgentForm {...props} />) };
}

describe("CreateAgentForm", () => {
  it("asks for a provider and a model, not just a name", () => {
    // The whole point. The form used to send a name alone, so the API resolved
    // the provider from DEFAULT_LLM_PROVIDER and every agent silently started
    // on `fake`.
    renderForm();
    expect(screen.getByLabelText(/name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/provider/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/model/i)).toBeInTheDocument();
  });

  it("offers only providers whose API key is set", () => {
    renderForm();
    expect(screen.getByRole("option", { name: /OpenAI/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /OpenRouter/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Anthropic/ })).not.toBeInTheDocument();
  });

  it("never offers the offline provider", () => {
    // An agent that replies with a canned string is not what anyone is
    // creating, however convenient it is for the server's tests.
    renderForm();
    expect(screen.queryByRole("option", { name: /Fake/i })).not.toBeInTheDocument();
  });

  it("cannot be submitted before a model is chosen", () => {
    // The model picker starts empty; submitting would send "" and be rejected
    // by the API. Better to not let the button arm at all.
    const { props } = renderForm();
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "Sales Bot" } });
    fireEvent.click(screen.getByRole("button", { name: /create agent/i }));
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it("cannot be submitted without a name", () => {
    const { props } = renderForm();
    fireEvent.change(screen.getByLabelText(/model/i), { target: { value: "gpt-4o-mini" } });
    fireEvent.click(screen.getByRole("button", { name: /create agent/i }));
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it("submits the name, provider and model together", () => {
    const { props } = renderForm();
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "Sales Bot" } });
    fireEvent.change(screen.getByLabelText(/model/i), { target: { value: "gpt-4o-mini" } });
    fireEvent.click(screen.getByRole("button", { name: /create agent/i }));
    expect(props.onSubmit).toHaveBeenCalledWith({
      name: "Sales Bot",
      provider: "openai",
      model: "gpt-4o-mini",
    });
  });

  it("clears the chosen model when the provider changes", () => {
    // A model id belongs to one provider. Carrying `gpt-4o-mini` over to
    // Anthropic would create an agent that cannot answer.
    const { props } = renderForm();
    fireEvent.change(screen.getByLabelText(/model/i), { target: { value: "gpt-4o-mini" } });
    fireEvent.change(screen.getByLabelText(/provider/i), { target: { value: "openrouter" } });
    expect((screen.getByLabelText(/model/i) as HTMLSelectElement).value).toBe("");
    expect(props.onProviderChange).toHaveBeenCalledWith("openrouter");
  });

  it("explains itself instead of rendering an empty dropdown when no key is set", () => {
    // Nothing can be created at all in this state, and an empty select gives
    // the user no idea why.
    renderForm({ providers: [{ id: "fake", configured: true }], provider: "" });
    expect(screen.getByRole("alert")).toHaveTextContent(/API_KEY/);
    expect(screen.getByRole("button", { name: /create agent/i })).toBeDisabled();
  });

  it("shows the API's error message when the create failed", () => {
    renderForm({ error: "an agent named 'Sales Bot' already exists" });
    expect(screen.getByText(/already exists/)).toBeInTheDocument();
  });
});
