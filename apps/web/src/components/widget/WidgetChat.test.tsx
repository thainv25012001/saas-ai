// @vitest-environment happy-dom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/widget-api";
import type { WidgetEvent, WidgetSession } from "@/lib/widget-api";
import { WidgetChat } from "./WidgetChat";

vi.mock("@/lib/widget-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/widget-api")>();
  return {
    ...actual,
    readStoredToken: vi.fn(),
    storeToken: vi.fn(),
    startSession: vi.fn(),
    loadConversation: vi.fn(),
    streamWidgetChat: vi.fn(),
  };
});

const readStoredToken = vi.mocked(api.readStoredToken);
const storeToken = vi.mocked(api.storeToken);
const startSession = vi.mocked(api.startSession);
const loadConversation = vi.mocked(api.loadConversation);
const streamWidgetChat = vi.mocked(api.streamWidgetChat);

const SESSION: WidgetSession = {
  token: "tok-new",
  expiresAt: "2026-10-25T00:00:00Z",
  config: {
    agentName: "Ava",
    title: "Ask Acme",
    greeting: "Hi! How can I help?",
    fallbackMessage: "Sorry, something went wrong on our side.",
    brandColor: "#0f766e",
    position: "left",
  },
};

const UNAVAILABLE = "This assistant is not available right now.";

type StreamParams = Parameters<typeof api.streamWidgetChat>[0];

/** A stream the test drives event by event. */
function controllableStream() {
  let params: StreamParams | null = null;
  let finish: () => void = () => {};
  streamWidgetChat.mockImplementation((p) => {
    params = p;
    return new Promise<void>((resolve) => {
      finish = resolve;
    });
  });
  return {
    emit(event: WidgetEvent) {
      act(() => {
        params?.onEvent(event);
      });
    },
    async end() {
      await act(async () => {
        finish();
      });
    },
    params: () => params,
  };
}

function renderChat() {
  return render(<WidgetChat apiUrl="http://api" publicKey="pk_test" />);
}

async function send(text: string) {
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

let postMessage: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  readStoredToken.mockReturnValue("tok-old");
  startSession.mockResolvedValue(SESSION);
  loadConversation.mockResolvedValue(null);
  postMessage = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
});

afterEach(() => {
  vi.clearAllMocks();
  postMessage.mockRestore();
});

describe("WidgetChat", () => {
  it("starts a session with the stored token, stores the new one, and shows the greeting", async () => {
    renderChat();

    expect(await screen.findByText("Hi! How can I help?")).toBeInTheDocument();
    expect(startSession).toHaveBeenCalledWith("http://api", "pk_test", "tok-old");
    expect(storeToken).toHaveBeenCalledWith("pk_test", "tok-new");
    expect(loadConversation).toHaveBeenCalledWith("http://api", "tok-new");
    expect(screen.getByRole("heading", { name: "Ask Acme" })).toBeInTheDocument();
  });

  it("falls back to the agent name when there is no title", async () => {
    startSession.mockResolvedValue({ ...SESSION, config: { ...SESSION.config, title: null } });
    renderChat();
    expect(await screen.findByRole("heading", { name: "Ava" })).toBeInTheDocument();
  });

  it("renders a resumed conversation instead of the greeting", async () => {
    loadConversation.mockResolvedValue({
      conversationId: "c-old",
      messages: [
        { role: "user", text: "Do you ship to Hanoi?" },
        { role: "assistant", text: "Yes, **free** over $50." },
      ],
    });
    renderChat();

    expect(await screen.findByText("Do you ship to Hanoi?")).toBeInTheDocument();
    // Assistant text goes through AnswerText's restricted markdown.
    expect(screen.getByText("free").tagName).toBe("STRONG");
    expect(screen.queryByText("Hi! How can I help?")).not.toBeInTheDocument();
  });

  it("renders user text as plain text, never markup", async () => {
    loadConversation.mockResolvedValue({
      conversationId: "c-old",
      messages: [{ role: "user", text: "<script>alert(1)</script> **not bold**" }],
    });
    const { container } = renderChat();

    expect(await screen.findByText("<script>alert(1)</script> **not bold**")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("strong")).toBeNull();
  });

  it("sends a message and streams the reply", async () => {
    const stream = controllableStream();
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await send("What do you sell?");

    expect(screen.getByText("What do you sell?")).toBeInTheDocument();
    expect(stream.params()).toMatchObject({
      apiUrl: "http://api",
      token: "tok-new",
      message: "What do you sell?",
      conversationId: null,
    });

    stream.emit({ type: "message_start", conversation_id: "c1", message_id: "m1" });
    stream.emit({ type: "text_delta", text: "We sell " });
    stream.emit({ type: "text_delta", text: "tea." });
    stream.emit({ type: "citations", citations: [{ document_title: "Catalogue", page: 2 }] });
    stream.emit({ type: "message_end" });
    await stream.end();

    expect(screen.getByText("We sell tea.")).toBeInTheDocument();
    expect(screen.getByText("Sources: Catalogue (p. 2)")).toBeInTheDocument();
  });

  it("continues the same conversation on the next turn", async () => {
    const stream = controllableStream();
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await send("first");
    stream.emit({ type: "message_start", conversation_id: "c1", message_id: "m1" });
    stream.emit({ type: "message_end" });
    await stream.end();

    await send("second");
    expect(stream.params()?.conversationId).toBe("c1");
  });

  it("shows a tool label while a tool runs, and drops it on the next text", async () => {
    const stream = controllableStream();
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await send("Any green tea?");
    stream.emit({ type: "tool_call_start", calls: [{ name: "search_products" }] });
    expect(screen.getByText("Searching products…")).toBeInTheDocument();

    stream.emit({ type: "text_delta", text: "Yes, sencha." });
    expect(screen.queryByText("Searching products…")).not.toBeInTheDocument();
    expect(screen.getByText("Yes, sencha.")).toBeInTheDocument();
  });

  it("shows the agent's fallback message on an error", async () => {
    const stream = controllableStream();
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await send("hello");
    stream.emit({ type: "error", code: "internal_error", message: "Something went wrong." });
    await stream.end();

    expect(screen.getByText("Sorry, something went wrong on our side.")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong.")).not.toBeInTheDocument();
  });

  it("says the assistant is unavailable when the daily cap is reached", async () => {
    const stream = controllableStream();
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await send("hello");
    stream.emit({ type: "error", code: "widget_daily_cap", message: "not available" });
    await stream.end();

    expect(screen.getByText(UNAVAILABLE)).toBeInTheDocument();
  });

  it("says the assistant is unavailable when the session is refused", async () => {
    startSession.mockRejectedValue(new api.WidgetUnavailableError("widget not found"));
    renderChat();

    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
  });

  it("posts ready with the public config to the parent", async () => {
    renderChat();
    await screen.findByText("Hi! How can I help?");

    expect(postMessage).toHaveBeenCalledWith(
      { type: "ready", brand_color: "#0f766e", position: "left", title: "Ask Acme" },
      "*",
    );
  });

  it("applies the brand colour as a custom property, not a class", async () => {
    const { container } = renderChat();
    await screen.findByText("Hi! How can I help?");

    const root = container.firstElementChild as HTMLElement;
    expect(root.style.getPropertyValue("--widget-brand")).toBe("#0f766e");
    expect(container.innerHTML).not.toContain("bg-[#0f766e]");
  });

  it("puts black text on a pale brand colour and white on a dark one", async () => {
    startSession.mockResolvedValue({
      ...SESSION,
      config: { ...SESSION.config, brandColor: "#fef08a" },
    });
    const { container, unmount } = renderChat();
    await screen.findByText("Hi! How can I help?");

    const root = container.firstElementChild as HTMLElement;
    expect(root.style.getPropertyValue("--widget-brand-ink")).toBe("#000000");
    // Header, user bubbles and Send all read it through one fixed class.
    const inkClass = "text-[var(--widget-brand-ink,var(--color-primary-ink))]";
    expect(screen.getByRole("banner").className).toContain(inkClass);
    expect(screen.getByRole("button", { name: "Send" }).className).toContain(inkClass);
    unmount();

    startSession.mockResolvedValue(SESSION);
    const dark = renderChat();
    await screen.findByText("Hi! How can I help?");
    const darkRoot = dark.container.firstElementChild as HTMLElement;
    expect(darkRoot.style.getPropertyValue("--widget-brand-ink")).toBe("#ffffff");
  });

  it("posts close to the parent from the close button", async () => {
    renderChat();
    await screen.findByText("Hi! How can I help?");

    fireEvent.click(screen.getByRole("button", { name: "Close chat" }));
    expect(postMessage).toHaveBeenCalledWith({ type: "close" }, "*");
  });

  it("starts a new conversation: clears the thread and sends with no conversation id", async () => {
    loadConversation.mockResolvedValue({
      conversationId: "c-old",
      messages: [{ role: "user", text: "old question" }],
    });
    const stream = controllableStream();
    renderChat();
    await screen.findByText("old question");

    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.queryByText("old question")).not.toBeInTheDocument();
    expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument();

    await send("fresh start");
    expect(stream.params()?.conversationId).toBeNull();
  });

  it("shows a counter only past 1800 characters and caps the message at 2000", async () => {
    renderChat();
    await screen.findByText("Hi! How can I help?");
    const box = screen.getByLabelText("Message");

    fireEvent.change(box, { target: { value: "a".repeat(1800) } });
    expect(screen.queryByText(/\/ 2000/)).not.toBeInTheDocument();

    fireEvent.change(box, { target: { value: "a".repeat(1801) } });
    expect(screen.getByText("1801 / 2000")).toBeInTheDocument();
    expect(box).toHaveAttribute("maxLength", "2000");
  });

  it("does not send an empty message", async () => {
    renderChat();
    await screen.findByText("Hi! How can I help?");

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await waitFor(() => expect(streamWidgetChat).not.toHaveBeenCalled());
  });

  it("shows the header, with a working close button, while loading", async () => {
    startSession.mockReturnValue(new Promise<WidgetSession>(() => {}));
    renderChat();

    expect(screen.getByRole("heading", { name: "Chat" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close chat" }));
    expect(postMessage).toHaveBeenCalledWith({ type: "close" }, "*");
  });

  it("shows the header, with a working close button, when unavailable", async () => {
    startSession.mockRejectedValue(new api.WidgetUnavailableError());
    renderChat();

    await screen.findByText(UNAVAILABLE);
    fireEvent.click(screen.getByRole("button", { name: "Close chat" }));
    expect(postMessage).toHaveBeenCalledWith({ type: "close" }, "*");
  });

  it("says the assistant is unavailable when the session request times out", async () => {
    startSession.mockRejectedValue(new api.WidgetTimeoutError());
    renderChat();

    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument();
  });

  it("says the assistant is unavailable when loading the conversation times out", async () => {
    loadConversation.mockRejectedValue(new api.WidgetTimeoutError());
    renderChat();

    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
  });

  it("shows the fallback, not an unhandled rejection, when the stream throws", async () => {
    streamWidgetChat.mockRejectedValue(new Error("boom"));
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await act(async () => {
      await send("hello");
    });

    expect(await screen.findByText("Sorry, something went wrong on our side.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled(); // empty draft
  });

  it("keeps partial streamed text and appends the fallback after it on an error", async () => {
    const stream = controllableStream();
    const { container } = renderChat();
    await screen.findByText("Hi! How can I help?");

    await send("hello");
    stream.emit({ type: "text_delta", text: "We sell green" });
    stream.emit({ type: "error", code: "internal_error", message: "Something went wrong." });
    await stream.end();

    const log = container.querySelector('[role="log"]')?.textContent ?? "";
    expect(log).toContain("We sell green");
    expect(log).toContain("Sorry, something went wrong on our side.");
    expect(log.indexOf("We sell green")).toBeLessThan(
      log.indexOf("Sorry, something went wrong on our side."),
    );
  });

  it("retries once as a new conversation when a resumed conversation is not found", async () => {
    loadConversation.mockResolvedValue({
      conversationId: "c-gone",
      messages: [{ role: "user", text: "old question" }],
    });
    const calls: StreamParams[] = [];
    streamWidgetChat.mockImplementation(async (p) => {
      calls.push(p);
      if (calls.length === 1) {
        p.onEvent({ type: "error", code: "not_found", message: "conversation not found" });
        return;
      }
      p.onEvent({ type: "message_start", conversation_id: "c-new", message_id: "m1" });
      p.onEvent({ type: "text_delta", text: "Fresh answer." });
      p.onEvent({ type: "message_end" });
    });
    renderChat();
    await screen.findByText("old question");

    await act(async () => {
      await send("still there?");
    });

    expect(await screen.findByText("Fresh answer.")).toBeInTheDocument();
    expect(calls.map((c) => c.conversationId)).toEqual(["c-gone", null]);
    expect(calls[1].message).toBe("still there?");
    expect(screen.queryByText(UNAVAILABLE)).not.toBeInTheDocument();
    expect(screen.getAllByText("still there?")).toHaveLength(1);

    // The next turn continues the new conversation.
    await act(async () => {
      await send("and now?");
    });
    expect(calls[2].conversationId).toBe("c-new");
  });

  it("does not retry a not_found on a turn that sent no conversation id", async () => {
    const calls: StreamParams[] = [];
    streamWidgetChat.mockImplementation(async (p) => {
      calls.push(p);
      p.onEvent({ type: "error", code: "not_found", message: "widget not found" });
    });
    renderChat();
    await screen.findByText("Hi! How can I help?");

    await act(async () => {
      await send("hello");
    });

    expect(calls).toHaveLength(1);
    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument();
  });

  it("retries only once: a second not_found says unavailable", async () => {
    loadConversation.mockResolvedValue({
      conversationId: "c-gone",
      messages: [{ role: "user", text: "old question" }],
    });
    const calls: StreamParams[] = [];
    streamWidgetChat.mockImplementation(async (p) => {
      calls.push(p);
      p.onEvent({ type: "error", code: "not_found", message: "widget not found" });
    });
    renderChat();
    await screen.findByText("old question");

    await act(async () => {
      await send("hello");
    });

    expect(calls.map((c) => c.conversationId)).toEqual(["c-gone", null]);
    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument();
  });
});
