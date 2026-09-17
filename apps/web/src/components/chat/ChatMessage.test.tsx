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
            { chunkId: "c2", documentId: "d2", documentTitle: "FAQ.md", rank: 2, score: 0.5, excerpt: "second" },
            { chunkId: "c1", documentId: "d1", documentTitle: "Pricing.pdf", rank: 1, score: 0.9, excerpt: "first" },
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
