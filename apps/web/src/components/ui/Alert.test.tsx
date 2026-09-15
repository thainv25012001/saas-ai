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
