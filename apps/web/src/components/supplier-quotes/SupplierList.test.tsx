// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SupplierList, type SupplierRow } from "./SupplierList";

function supplier(overrides: Partial<SupplierRow> = {}): SupplierRow {
  return {
    id: "s1",
    name: "Acme",
    senderEmails: ["quotes@acme.example"],
    senderDomains: ["acme.example"],
    isActive: true,
    ...overrides,
  };
}

const base = { saving: false, error: null, onSave: () => {} };

describe("SupplierList", () => {
  it("explains how suppliers are recognised when there are none", () => {
    render(<SupplierList {...base} suppliers={[]} />);
    expect(screen.getByText(/no suppliers yet/i)).toBeInTheDocument();
  });

  it("lists a supplier's sender rules", () => {
    render(<SupplierList {...base} suppliers={[supplier({ isActive: false })]} />);
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("quotes@acme.example")).toBeInTheDocument();
    expect(screen.getByText("@acme.example")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });

  it("creates a supplier from free-text sender rules", () => {
    const onSave = vi.fn();
    render(<SupplierList {...base} suppliers={[]} onSave={onSave} />);
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: " Beta Parts " } });
    fireEvent.change(screen.getByLabelText(/sender addresses/i), {
      target: { value: "Sales@Beta.example\nsales@beta.example, offers@beta.example" },
    });
    fireEvent.change(screen.getByLabelText(/sender domains/i), { target: { value: "beta.example" } });
    fireEvent.click(screen.getByRole("button", { name: /add supplier/i }));
    expect(onSave).toHaveBeenCalledWith(null, {
      name: "Beta Parts",
      senderEmails: ["sales@beta.example", "offers@beta.example"],
      senderDomains: ["beta.example"],
      isActive: true,
    });
  });

  it("edits an existing supplier in the same form", () => {
    const onSave = vi.fn();
    render(<SupplierList {...base} suppliers={[supplier()]} onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: /edit acme/i }));
    expect(screen.getByLabelText(/^name/i)).toHaveValue("Acme");
    fireEvent.click(screen.getByLabelText(/recognise emails from this supplier/i));
    fireEvent.click(screen.getByRole("button", { name: /save supplier/i }));
    expect(onSave).toHaveBeenCalledWith("s1", {
      name: "Acme",
      senderEmails: ["quotes@acme.example"],
      senderDomains: ["acme.example"],
      isActive: false,
    });
  });

  it("does not submit without a name and shows the server's error", () => {
    const onSave = vi.fn();
    render(<SupplierList {...base} suppliers={[]} onSave={onSave} error="supplier name already exists" />);
    expect(screen.getByRole("button", { name: /add supplier/i })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("supplier name already exists");
  });
});
