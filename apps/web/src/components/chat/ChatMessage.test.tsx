// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChatMessage, type ChatMessageData } from "./ChatMessage";

const assistant = (overrides: Partial<ChatMessageData> = {}): ChatMessageData => ({
  id: "m1",
  role: "assistant",
  text: "Our starter plan is $19 a month.",
  status: "done",
  ...overrides,
});

describe("ChatMessage", () => {
  it("says a turn is not priced rather than showing it as free", () => {
    render(
      <ChatMessage
        message={assistant({
          meta: {
            model: "some-new-model",
            usage: { input_tokens: 12, output_tokens: 34 },
            costUsd: null,
            latencyMs: 820,
          },
        })}
      />,
    );
    expect(screen.getByText("not priced")).toBeInTheDocument();
  });

  it("formats a known cost in dollars", () => {
    render(
      <ChatMessage
        message={assistant({
          meta: {
            model: "gpt-4o-mini",
            usage: { input_tokens: 12, output_tokens: 34 },
            costUsd: "0.0012",
            latencyMs: 820,
          },
        })}
      />,
    );
    expect(screen.getByText("$0.0012")).toBeInTheDocument();
  });

  it("explains an assistant turn that was stopped before any token arrived", () => {
    render(<ChatMessage message={assistant({ text: "" })} />);
    expect(screen.getByText(/Stopped before any response arrived/)).toBeInTheDocument();
  });

  it("numbers citations by rank, sorted, not by array order", () => {
    render(
      <ChatMessage
        message={assistant({
          citations: [
            {
              chunkId: "c2",
              documentId: "d2",
              documentTitle: "FAQ.md",
              rank: 2,
              score: 0.5,
              excerpt: "second",
              page: null,
            },
            {
              chunkId: "c1",
              documentId: "d1",
              documentTitle: "Pricing.pdf",
              rank: 1,
              score: 0.9,
              excerpt: "first",
              page: 4,
            },
          ],
        })}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("1.");
    expect(items[0]).toHaveTextContent("Pricing.pdf");
    expect(items[1]).toHaveTextContent("2.");
    expect(items[1]).toHaveTextContent("FAQ.md");
  });

  it("shows the page for a paginated source and omits it for a non-paginated one", () => {
    render(
      <ChatMessage
        message={assistant({
          citations: [
            {
              chunkId: "c1",
              documentId: "d1",
              documentTitle: "Pricing.pdf",
              rank: 1,
              score: 0.9,
              excerpt: "first",
              page: 4,
            },
            {
              chunkId: "c2",
              documentId: "d2",
              documentTitle: "FAQ.md",
              rank: 2,
              score: 0.5,
              excerpt: "second",
              page: null,
            },
          ],
        })}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("page 4");
    // Most corpora are not PDFs -- absent must render as nothing, not as a
    // literal "page null" or an empty "page" label.
    expect(items[1]).not.toHaveTextContent(/page/i);
  });

  it("renders no Sources section when there are no citations yet", () => {
    render(<ChatMessage message={assistant({ citations: [] })} />);
    expect(screen.queryByText("Sources")).not.toBeInTheDocument();
  });

  it("renders an untrusted document title and excerpt as literal text, never as markup", () => {
    // Both fields come from a file the customer uploaded, and the backend
    // deliberately does not escape them (see the module docstring on
    // `Citation` in `@/lib/sse`). This test is what would fail if that
    // escaping were ever done with dangerouslySetInnerHTML or a markdown
    // renderer instead of a plain JSX text child.
    const { container } = render(
      <ChatMessage
        message={assistant({
          citations: [
            {
              chunkId: "c1",
              documentId: "d1",
              documentTitle: "<script>alert(1)</script>",
              rank: 1,
              score: 0.9,
              excerpt: "click <b>here</b> to win",
              page: null,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(screen.getByText(/click <b>here<\/b> to win/)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("renders a tool call inline, with its name and a readable argument summary", () => {
    render(
      <ChatMessage
        message={assistant({
          toolCalls: [
            {
              id: "call_1",
              name: "retrieve_knowledge",
              arguments: { query: "starter plan pricing" },
              status: "done",
              result: "Found 2 chunks.",
              isError: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("retrieve_knowledge")).toBeInTheDocument();
    expect(screen.getByText(/starter plan pricing/)).toBeInTheDocument();
  });

  it("renders an errored tool call as visibly distinct, not an empty success", () => {
    render(
      <ChatMessage
        message={assistant({
          toolCalls: [
            {
              id: "call_1",
              name: "create_lead",
              arguments: { name: "Jamie" },
              status: "done",
              result: "invalid arguments: at least one of 'email' or 'phone' is required",
              isError: true,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.queryByText("Done")).not.toBeInTheDocument();
  });

  it("renders a <script> payload in a tool call's arguments as literal text, never markup", () => {
    const { container } = render(
      <ChatMessage
        message={assistant({
          toolCalls: [
            {
              id: "call_1",
              name: "create_lead",
              arguments: { name: "<script>alert(1)</script>" },
              status: "done",
              result: "<script>alert(2)</script>",
              isError: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
    expect(screen.getByText("<script>alert(2)</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("shows a step-limit turn as a distinct, named outcome rather than a generic failure", () => {
    render(
      <ChatMessage
        message={assistant({
          text: "Let me check a few things.",
          status: "error",
          error: {
            code: "step_limit_reached",
            message: "The assistant reached its step limit (6) while still requesting tools.",
          },
        })}
      />,
    );
    // A status, not an alert -- see Alert.test.tsx's own reasoning: this
    // outcome must not interrupt a screen reader the way a real failure does.
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Reached its step limit");
    expect(status).toHaveTextContent(/while still requesting tools/);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(status).not.toHaveClass("border-danger-line");
  });

  it("shows a turn error as an alert while keeping the partial text", () => {
    render(
      <ChatMessage
        message={assistant({
          text: "Our starter plan",
          status: "error",
          error: { code: "provider_error", message: "The provider timed out." },
        })}
      />,
    );
    expect(screen.getByText("Our starter plan")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The provider timed out.");
  });
});
