// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelPicker, type ModelChoice } from "./ModelPicker";

const options: ModelChoice[] = [
  { id: "google/gemma-4-31b-it:free", label: "Google: Gemma 4 31B (free)", contextLength: 262144 },
  { id: "z-ai/glm-5.2:free", label: "Z.ai: GLM 5.2 (free)", contextLength: 32768 },
];

const noop = () => {};

describe("ModelPicker", () => {
  it("offers every model the provider returned", () => {
    render(<ModelPicker value={options[0].id} options={options} onChange={noop} />);
    expect(screen.getByRole("option", { name: /Gemma 4 31B/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /GLM 5.2/ })).toBeInTheDocument();
  });

  it("keeps the agent's current model selectable when the list no longer offers it", () => {
    // Otherwise opening an agent on a retired or paid model shows some OTHER
    // model as selected, and saving the form silently repoints the agent.
    render(<ModelPicker value="vendor/retired-model" options={options} onChange={noop} />);
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("vendor/retired-model");
    expect(screen.getByRole("option", { name: /vendor\/retired-model/ })).toBeInTheDocument();
  });

  it("falls back to a text field when the model list could not be loaded", () => {
    // An empty dropdown is a form that cannot be saved at all.
    render(<ModelPicker value="google/gemma-4-31b-it:free" options={[]} failed onChange={noop} />);
    expect(screen.getByRole("textbox")).toHaveValue("google/gemma-4-31b-it:free");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("says it is still loading rather than showing an empty list", () => {
    render(<ModelPicker value="" options={[]} fetching onChange={noop} />);
    expect(screen.getByRole("combobox")).toBeDisabled();
    // A disabled empty box is indistinguishable from a broken one. It has to
    // say which of the two it is.
    expect(screen.getByRole("combobox")).toHaveTextContent(/loading/i);
  });

  it("never displays a model the form is not holding", () => {
    // A native select with no matching value paints its FIRST option, so an
    // unset model showed "Google: Gemma 4 31B" while the form saved "".
    render(<ModelPicker value="" options={options} onChange={noop} />);
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("");
    expect(screen.getByRole("option", { name: /select a model/i })).toBeInTheDocument();
  });

  it("says so when the provider offers no models at all", () => {
    render(<ModelPicker value="" options={[]} onChange={noop} />);
    expect(screen.getByRole("combobox")).toHaveTextContent(/no models/i);
  });

  it("exposes the full model id for a label too long to fit the control", () => {
    // OpenRouter labels clip in the closed select; hover has to recover them.
    render(<ModelPicker value={options[0].id} options={options} onChange={noop} />);
    expect(screen.getByRole("combobox")).toHaveAttribute("title", options[0].id);
  });
});
