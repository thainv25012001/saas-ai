// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Input, Select } from "./Input";

describe("Select", () => {
  it("suppresses the browser's own arrow so the control matches Input", () => {
    // Without `appearance-none` the OS paints its own arrow flush at the right
    // edge, ignoring every token -- which is what made the dropdowns read as a
    // different control from the text inputs beside them.
    render(
      <Select value="a" onChange={() => {}}>
        <option value="a">A</option>
      </Select>,
    );
    expect(screen.getByRole("combobox").className).toContain("appearance-none");
  });

  it("draws the one chevron, which never eats a click meant for the select", () => {
    const { container } = render(
      <Select value="a" onChange={() => {}}>
        <option value="a">A</option>
      </Select>,
    );
    const chevron = container.querySelector("svg");
    expect(chevron).not.toBeNull();
    expect(chevron?.getAttribute("class")).toContain("pointer-events-none");
  });

  it("carries the same border, radius and focus ring as Input", () => {
    // The whole point of `controlClasses`: a Select and an Input side by side
    // must not drift apart.
    const { container: selectBox } = render(
      <Select value="a" onChange={() => {}}>
        <option value="a">A</option>
      </Select>,
    );
    const { container: inputBox } = render(<Input readOnly value="" />);
    const shared = ["rounded-control", "border-line-strong", "focus-visible:ring-2"];
    const selectClasses = selectBox.querySelector("select")!.className;
    const inputClasses = inputBox.querySelector("input")!.className;
    for (const token of shared) {
      expect(selectClasses).toContain(token);
      expect(inputClasses).toContain(token);
    }
  });

  it("keeps a caller's own classes on the select itself", () => {
    // The playground sizes its agent picker with `min-w-48`; wrapping the
    // select must not strand that class on the wrapper.
    render(
      <Select value="a" onChange={() => {}} width="auto" className="min-w-48">
        <option value="a">A</option>
      </Select>,
    );
    expect(screen.getByRole("combobox").className).toContain("min-w-48");
  });
});
