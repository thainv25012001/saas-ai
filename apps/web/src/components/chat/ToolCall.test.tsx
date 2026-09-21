// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolCall, type ToolCallData } from "./ToolCall";

const running = (overrides: Partial<ToolCallData> = {}): ToolCallData => ({
  id: "call_1",
  name: "retrieve_knowledge",
  arguments: { query: "pricing" },
  status: "running",
  ...overrides,
});

describe("ToolCall", () => {
  it("renders the tool's name and a readable argument summary", () => {
    render(<ToolCall data={running()} />);
    expect(screen.getByText("retrieve_knowledge")).toBeInTheDocument();
    expect(screen.getByText(/query: pricing/)).toBeInTheDocument();
  });

  it("shows a running call as in progress, with no result to expand yet", () => {
    render(<ToolCall data={running()} />);
    expect(screen.getByText("Running…")).toBeInTheDocument();
    expect(screen.queryByText("Result")).not.toBeInTheDocument();
  });

  it("shows a successful call as done, with its result collapsed behind a summary", () => {
    render(
      <ToolCall
        data={running({ status: "done", result: "3 chunks found", isError: false })}
      />,
    );
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByText("3 chunks found")).toBeInTheDocument();
  });

  it("renders a failed call as visibly distinct from a successful one, not an empty success", () => {
    const { container } = render(
      <ToolCall
        data={running({
          name: "create_lead",
          status: "done",
          result: "invalid arguments: 'interest' is required",
          isError: true,
        })}
      />,
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.queryByText("Done")).not.toBeInTheDocument();
    // Not just a different label -- a different visual tone, so scanning a
    // transcript full of tool calls does not require reading every one.
    expect(container.firstChild).toHaveClass("border-danger-line");
    expect(container.firstChild).not.toHaveClass("border-line");
  });

  it("renders a <script> payload in arguments and result as literal text, never markup", () => {
    // Both fields are model output that can itself quote an uploaded
    // document (retrieve_knowledge) or a visitor's own typed text
    // (create_lead) -- this is what would fail if either were ever rendered
    // through dangerouslySetInnerHTML or a markdown pass instead of a plain
    // JSX text child.
    const { container } = render(
      <ToolCall
        data={running({
          name: "create_lead",
          arguments: { name: "<script>alert(1)</script>", interest: "click <b>here</b>" },
          status: "done",
          result: "<img src=x onerror=alert(1)>",
          isError: false,
        })}
      />,
    );
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
    expect(screen.getByText(/click <b>here<\/b>/)).toBeInTheDocument();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("summarises a call with no arguments rather than an empty string", () => {
    render(<ToolCall data={running({ arguments: {} })} />);
    expect(screen.getByText("no arguments")).toBeInTheDocument();
  });
});
