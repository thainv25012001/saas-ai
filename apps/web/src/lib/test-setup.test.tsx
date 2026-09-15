// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { cn } from "@/components/ui/cn";

describe("component test environment", () => {
  it("renders JSX into a DOM and matches with jest-dom", () => {
    render(<p data-testid="probe">hello</p>);
    expect(screen.getByTestId("probe")).toBeInTheDocument();
  });

  it("resolves the @/ alias to src/", () => {
    expect(cn("a", false, "b", undefined)).toBe("a b");
  });
});
