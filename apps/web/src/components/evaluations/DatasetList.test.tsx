// @vitest-environment happy-dom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreateDatasetForm, DatasetList, type DatasetRow } from "./DatasetList";

function dataset(overrides: Partial<DatasetRow> = {}): DatasetRow {
  return { id: "d1", name: "Pricing", description: null, caseCount: 3, latestRun: null, ...overrides };
}

describe("DatasetList", () => {
  it("explains what a dataset is for when there are none", () => {
    render(<DatasetList datasets={[]} />);
    expect(screen.getByText("No datasets yet")).toBeInTheDocument();
    expect(screen.getByText(/questions with the answers you expect/)).toBeInTheDocument();
  });

  it("links each dataset and shows its case count and latest run", () => {
    render(
      <DatasetList
        datasets={[
          dataset({ latestRun: { status: "COMPLETED", passRate: 0.75 } }),
          dataset({ id: "d2", name: "Warranty", latestRun: null }),
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "Pricing" })).toHaveAttribute("href", "/dashboard/evaluations/d1");
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("75% passed")).toBeInTheDocument();
    expect(screen.getByText("Never run")).toBeInTheDocument();
  });

  it("renders a dataset name as text, never markup", () => {
    const { container } = render(<DatasetList datasets={[dataset({ name: "<script>alert(1)</script>" })]} />);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
  });
});

describe("CreateDatasetForm", () => {
  it("submits a trimmed name and clears itself on success", async () => {
    const onCreate = vi.fn(async () => true);
    render(<CreateDatasetForm onCreate={onCreate} />);
    const name = screen.getByLabelText(/Name/);
    fireEvent.change(name, { target: { value: "  Pricing  " } });
    fireEvent.click(screen.getByRole("button", { name: "Create dataset" }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith({ name: "Pricing", description: null }));
    await waitFor(() => expect(name).toHaveValue(""));
  });

  it("cannot submit a blank name", () => {
    render(<CreateDatasetForm onCreate={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Create dataset" })).toBeDisabled();
  });
});
