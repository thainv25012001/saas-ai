// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Field } from "./Field";
import { Input } from "./Input";

describe("Field", () => {
  it("associates the label with the control", () => {
    render(<Field label="Agent name">{(control) => <Input {...control} />}</Field>);
    // getByLabelText only finds it if htmlFor and id actually match.
    expect(screen.getByLabelText("Agent name")).toBeInTheDocument();
  });

  it("describes the control with its description text", () => {
    render(
      <Field label="Provider" description="`fake` answers offline and costs nothing.">
        {(control) => <Input {...control} />}
      </Field>,
    );
    expect(screen.getByLabelText("Provider")).toHaveAccessibleDescription(
      "`fake` answers offline and costs nothing.",
    );
  });

  it("marks the control invalid and describes it with the error", () => {
    render(
      <Field label="Model" description="Must exist for the provider." error="Unknown model.">
        {(control) => <Input {...control} />}
      </Field>,
    );
    const input = screen.getByLabelText("Model");
    expect(input).toHaveAttribute("aria-invalid", "true");
    // Both the description and the error are announced, in that order.
    expect(input).toHaveAccessibleDescription("Must exist for the provider. Unknown model.");
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown model.");
  });

  it("sets no aria-describedby when there is nothing to describe", () => {
    render(<Field label="Tone">{(control) => <Input {...control} />}</Field>);
    expect(screen.getByLabelText("Tone")).not.toHaveAttribute("aria-describedby");
  });

  it("passes required through to the control", () => {
    render(<Field label="Email" required>{(control) => <Input {...control} />}</Field>);
    expect(screen.getByLabelText(/Email/)).toBeRequired();
  });
});
