// @vitest-environment happy-dom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CaseForm, EXPECTATION_REQUIRED_MESSAGE } from "./CaseForm";

const DOCS = [{ id: "doc-1", title: "Warranty.pdf" }];

function renderForm(overrides: Partial<React.ComponentProps<typeof CaseForm>> = {}) {
  const onSubmit = vi.fn(async () => true);
  const onProductSearch = vi.fn();
  render(
    <CaseForm
      documents={DOCS}
      productOptions={[]}
      onProductSearch={onProductSearch}
      onSubmit={onSubmit}
      {...overrides}
    />,
  );
  return { onSubmit, onProductSearch };
}

describe("CaseForm", () => {
  it("blocks a submit with no expectation and says why", async () => {
    const { onSubmit } = renderForm();
    fireEvent.change(screen.getByLabelText(/Question/), { target: { value: "What is covered?" } });
    // Tags are not an expectation.
    fireEvent.change(screen.getByLabelText(/Tags/), { target: { value: "warranty" } });
    fireEvent.click(screen.getByRole("button", { name: "Add case" }));

    expect(await screen.findByText(EXPECTATION_REQUIRED_MESSAGE)).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("blocks a submit with no question", async () => {
    const { onSubmit } = renderForm();
    fireEvent.change(screen.getByLabelText(/Reference answer/), { target: { value: "Three years." } });
    fireEvent.click(screen.getByRole("button", { name: "Add case" }));
    expect(await screen.findByText("A case needs a question.")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits every expectation, one phrase per line", async () => {
    const { onSubmit } = renderForm();
    fireEvent.change(screen.getByLabelText(/Question/), { target: { value: " What is covered? " } });
    fireEvent.change(screen.getByLabelText(/Required phrases/), { target: { value: "3 years\n\nparts" } });
    fireEvent.click(screen.getByLabelText("retrieve_knowledge"));
    fireEvent.click(screen.getByLabelText("Warranty.pdf"));
    fireEvent.change(screen.getByLabelText(/Tags/), { target: { value: "warranty, faq" } });
    fireEvent.click(screen.getByRole("button", { name: "Add case" }));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({
        question: "What is covered?",
        referenceAnswer: null,
        requiredPhrases: ["3 years", "parts"],
        expectedToolNames: ["retrieve_knowledge"],
        expectedDocumentIds: ["doc-1"],
        expectedProductIds: [],
        tags: ["warranty", "faq"],
      }),
    );
    expect(screen.queryByText(EXPECTATION_REQUIRED_MESSAGE)).toBeNull();
  });

  it("searches products and adds one as an expectation", async () => {
    const { onSubmit, onProductSearch } = renderForm({
      productOptions: [{ id: "p-1", name: "Model S", externalId: "SKU-1" }],
    });
    fireEvent.change(screen.getByLabelText("Search products to expect"), { target: { value: "model" } });
    expect(onProductSearch).toHaveBeenLastCalledWith("model");
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByRole("button", { name: "Remove Model S" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Question/), { target: { value: "Price of the Model S?" } });
    fireEvent.click(screen.getByRole("button", { name: "Add case" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ expectedProductIds: ["p-1"] })),
    );
  });

  it("prefills an existing case for editing", () => {
    renderForm({
      submitLabel: "Save case",
      initial: {
        question: "Old question",
        referenceAnswer: "Old answer",
        requiredPhrases: ["a", "b"],
        expectedToolNames: ["search_products"],
        expectedDocumentIds: [],
        expectedProductIds: [],
        tags: [],
      },
    });
    expect(screen.getByLabelText(/Question/)).toHaveValue("Old question");
    expect(screen.getByLabelText(/Required phrases/)).toHaveValue("a\nb");
    expect(screen.getByLabelText("search_products")).toBeChecked();
    expect(screen.getByRole("button", { name: "Save case" })).toBeInTheDocument();
  });
});
