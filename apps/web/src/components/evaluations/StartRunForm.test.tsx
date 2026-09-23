// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type ModelsState, StartRunForm, type VersionsState } from "./StartRunForm";

const AGENTS = [{ id: "agent-1", name: "Sales bot", provider: "fake", model: "fake-1" }];
const PROVIDERS = [
  { id: "fake", configured: true },
  { id: "openai", configured: true },
  { id: "anthropic", configured: false },
];
const VERSIONS: VersionsState = {
  versions: [
    { id: "v3", version: 3, isActive: false },
    { id: "v2", version: 2, isActive: true },
    { id: "v1", version: 1, isActive: false },
  ],
  fetching: false,
  failed: false,
  noPrompt: false,
};

const useModels = (provider: string): ModelsState => ({
  options: provider ? [{ id: `${provider}-model`, label: `${provider} model`, contextLength: null }] : [],
  fetching: false,
  failed: false,
});

function renderForm(overrides: Partial<React.ComponentProps<typeof StartRunForm>> = {}) {
  const onStart = vi.fn();
  render(
    <StartRunForm
      datasetId="ds-1"
      caseCount={3}
      agents={AGENTS}
      providers={PROVIDERS}
      useModels={useModels}
      useVersions={() => VERSIONS}
      onStart={onStart}
      {...overrides}
    />,
  );
  return { onStart };
}

describe("StartRunForm", () => {
  it("shows the call estimate, doubling it with a judge", () => {
    renderForm();
    expect(screen.getByText(/3 cases × 1 LLM call = 3 calls/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Grade answers with an LLM judge"));
    expect(screen.getByText(/3 cases × 2 LLM calls = 6 calls/)).toBeInTheDocument();
  });

  it("defaults to the active version, marks it, and sends it pinned", () => {
    const { onStart } = renderForm();
    const select = screen.getByLabelText("Prompt version");
    expect(select).toHaveValue("v2");
    expect(screen.getByRole("option", { name: "v2 (active)" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(onStart).toHaveBeenCalledWith({ dataset_id: "ds-1", agent_id: "agent-1", prompt_version_id: "v2" });
  });

  it("sends a draft version the user picked", () => {
    const { onStart } = renderForm();
    fireEvent.change(screen.getByLabelText("Prompt version"), { target: { value: "v3" } });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(onStart).toHaveBeenCalledWith(expect.objectContaining({ prompt_version_id: "v3" }));
  });

  it("sends a model override and a judge as complete pairs", () => {
    const { onStart } = renderForm();
    fireEvent.click(screen.getByLabelText("Use a different model for this run"));
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "openai" } });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "openai-model" } });
    fireEvent.click(screen.getByLabelText("Grade answers with an LLM judge"));
    fireEvent.change(screen.getByLabelText("Judge provider"), { target: { value: "openai" } });

    // A judge provider without a model is not a pair yet.
    expect(screen.getByRole("button", { name: "Start run" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Judge model"), { target: { value: "openai-model" } });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    expect(onStart).toHaveBeenCalledWith({
      dataset_id: "ds-1",
      agent_id: "agent-1",
      prompt_version_id: "v2",
      provider: "openai",
      model: "openai-model",
      judge_provider: "openai",
      judge_model: "openai-model",
    });
  });

  it("disables a provider with no API key", () => {
    renderForm();
    fireEvent.click(screen.getByLabelText("Grade answers with an LLM judge"));
    expect(screen.getByRole("option", { name: "Anthropic — no API key" })).toBeDisabled();
  });

  it("sends no version for an agent without a prompt", () => {
    const { onStart } = renderForm({
      useVersions: () => ({ versions: [], fetching: false, failed: false, noPrompt: true }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(onStart).toHaveBeenCalledWith({ dataset_id: "ds-1", agent_id: "agent-1" });
  });

  it("cannot start with no cases", () => {
    renderForm({ caseCount: 0 });
    expect(screen.getByRole("button", { name: "Start run" })).toBeDisabled();
  });

  it("says why a run cannot start while one is in progress", () => {
    renderForm({ blockedReason: "A run of this dataset is already in progress." });
    expect(screen.getByText("A run of this dataset is already in progress.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start run" })).toBeDisabled();
  });
});
