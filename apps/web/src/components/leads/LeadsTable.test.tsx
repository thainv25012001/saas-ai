// @vitest-environment happy-dom
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LeadsTable, type LeadRow } from "./LeadsTable";

function lead(overrides: Partial<LeadRow> = {}): LeadRow {
  return {
    id: "l1",
    agentId: "agent-1",
    name: "Jamie Rivera",
    email: "jamie@example.com",
    phone: null,
    interest: "the pro plan",
    status: "NEW",
    source: null,
    createdAt: "2026-01-01T00:00:00Z",
    conversation: { id: "c1", title: "Pricing questions", preview: null },
    ...overrides,
  };
}

describe("LeadsTable", () => {
  it("shows an empty state with no leads", () => {
    render(<LeadsTable leads={[]} />);
    expect(screen.getByText(/no leads yet/i)).toBeInTheDocument();
  });

  it("renders a lead's name, status and the conversation it came from", () => {
    render(<LeadsTable leads={[lead()]} />);
    expect(screen.getByText("Jamie Rivera")).toBeInTheDocument();
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.getByText("Pricing questions")).toBeInTheDocument();
    expect(screen.getByText(/jamie@example\.com/)).toBeInTheDocument();
  });

  it("falls back to the conversation's preview when it has no title yet", () => {
    render(
      <LeadsTable
        leads={[
          lead({
            conversation: { id: "c1", title: null, preview: "how much is the starter plan?" },
          }),
        ]}
      />,
    );
    expect(screen.getByText("how much is the starter plan?")).toBeInTheDocument();
  });

  it("says a partial capture has no contact info rather than showing a blank cell", () => {
    render(<LeadsTable leads={[lead({ email: null, phone: null })]} />);
    expect(screen.getByText("No contact info given")).toBeInTheDocument();
  });

  it("gives a lost lead a different tone from a won one", () => {
    // Two independent renders, not a rerender of the same tree: `LeadsTable`
    // reuses the same badge DOM node across a rerender in this position, so
    // comparing "the same element, mutated" would trivially always match --
    // this is what would actually fail if won/lost ever shared a tone.
    const { container: wonContainer } = render(<LeadsTable leads={[lead({ status: "WON" })]} />);
    const { container: lostContainer } = render(<LeadsTable leads={[lead({ status: "LOST" })]} />);
    const won = within(wonContainer).getByText("Won");
    const lost = within(lostContainer).getByText("Lost");
    expect(won.className).not.toBe(lost.className);
  });

  it("links the conversation cell to the conversations page for the lead's agent", () => {
    render(<LeadsTable leads={[lead({ agentId: "agent-9", conversation: { id: "c9", title: "Hi", preview: null } })]} />);
    const link = screen.getByRole("link", { name: "Hi" });
    expect(link).toHaveAttribute("href", "/dashboard/conversations?agent=agent-9&conversation=c9");
  });

  it("shows no link for a deleted conversation", () => {
    render(<LeadsTable leads={[lead({ conversation: null })]} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Deleted conversation")).toBeInTheDocument();
  });

  it("shows the lead's source as a badge when it is set", () => {
    render(<LeadsTable leads={[lead({ source: "widget" })]} />);
    expect(screen.getByText("Widget")).toBeInTheDocument();
  });

  it("shows no source badge when the source is null", () => {
    render(<LeadsTable leads={[lead({ source: null })]} />);
    expect(screen.queryByText("Widget")).not.toBeInTheDocument();
    expect(screen.queryByText("Playground")).not.toBeInTheDocument();
    expect(screen.queryByText("Api")).not.toBeInTheDocument();
  });

  it("renders an untrusted name and interest as literal text, never as markup", () => {
    // Both fields are copied from the model's own create_lead call, which in
    // turn quotes whatever the visitor typed -- same untrusted-text rule as
    // ChatMessage's citations and tool calls.
    const { container } = render(
      <LeadsTable
        leads={[
          lead({
            name: "<script>alert(1)</script>",
            interest: "click <b>here</b> to win",
          }),
        ]}
      />,
    );
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(screen.getByText("click <b>here</b> to win")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });
});
