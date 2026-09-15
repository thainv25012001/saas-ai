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
