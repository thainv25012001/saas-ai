import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TOOL_LABELS,
  WidgetTimeoutError,
  WidgetUnavailableError,
  loadConversation,
  readStoredToken,
  startSession,
  storeToken,
  streamWidgetChat,
  toWidgetEvent,
  toolLabel,
  type WidgetEvent,
} from "./widget-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("toWidgetEvent", () => {
  it.each<[unknown, WidgetEvent]>([
    [
      { type: "message_start", conversation_id: "c1", message_id: "m1" },
      { type: "message_start", conversation_id: "c1", message_id: "m1" },
    ],
    [{ type: "text_delta", text: "hi" }, { type: "text_delta", text: "hi" }],
    [
      { type: "tool_call_start", calls: [{ name: "search_products" }] },
      { type: "tool_call_start", calls: [{ name: "search_products" }] },
    ],
    [
      { type: "citations", citations: [{ document_title: "Guide", page: 3 }] },
      { type: "citations", citations: [{ document_title: "Guide", page: 3 }] },
    ],
    [
      { type: "citations", citations: [{ document_title: "Notes", page: null }] },
      { type: "citations", citations: [{ document_title: "Notes", page: null }] },
    ],
    [{ type: "message_end" }, { type: "message_end" }],
    [
      { type: "error", code: "internal_error", message: "Something went wrong." },
      { type: "error", code: "internal_error", message: "Something went wrong." },
    ],
  ])("accepts the projected shape %j", (raw, expected) => {
    expect(toWidgetEvent(raw)).toEqual(expected);
  });

  it("keeps only the projected fields when the server sends more", () => {
    expect(
      toWidgetEvent({
        type: "tool_call_start",
        calls: [{ id: "t1", name: "get_product", arguments: { id: "p" } }],
      }),
    ).toEqual({ type: "tool_call_start", calls: [{ name: "get_product" }] });
    expect(
      toWidgetEvent({ type: "message_end", model: "m", cost_usd: "0.1", usage: {} }),
    ).toEqual({ type: "message_end" });
  });

  it.each([
    ["a message_start without a message_id", { type: "message_start", conversation_id: "c1" }],
    ["a text_delta without text", { type: "text_delta" }],
    ["a tool_call_start whose call has no name", { type: "tool_call_start", calls: [{ id: "t1" }] }],
    ["a tool_call_start without calls", { type: "tool_call_start" }],
    [
      "a citation without a title (the playground's chunk-only shape)",
      { type: "citations", citations: [{ chunk_id: "k1", rank: 1, score: 0.5, page: 1 }] },
    ],
    ["a citation with a string page", { type: "citations", citations: [{ document_title: "G", page: "3" }] }],
    ["an error without a code", { type: "error", message: "x" }],
    ["a tool_call_end (never projected)", { type: "tool_call_end", results: [] }],
    ["an unknown type", { type: "ping" }],
    ["a non-object", "text_delta"],
    ["null", null],
  ])("rejects %s", (_label, raw) => {
    expect(toWidgetEvent(raw)).toBeNull();
  });
});

describe("TOOL_LABELS", () => {
  it("names each tool for a visitor, and anything else as working", () => {
    expect(TOOL_LABELS.search_products).toBe("Searching products…");
    expect(TOOL_LABELS.get_product).toBe("Looking up a product…");
    expect(TOOL_LABELS.retrieve_knowledge).toBe("Checking our information…");
    expect(TOOL_LABELS.create_lead).toBe("Saving your details…");
    expect(toolLabel("search_products")).toBe("Searching products…");
    expect(toolLabel("some_future_tool")).toBe("Working…");
    expect(toolLabel("toString")).toBe("Working…");
  });
});

const SESSION_BODY = {
  token: "tok-new",
  expires_at: "2026-10-25T00:00:00Z",
  config: {
    agent_name: "Ava",
    title: "Ask us",
    greeting: "Hi there!",
    fallback_message: "Sorry, try again later.",
    brand_color: "#123abc",
    position: "left",
  },
};

describe("startSession", () => {
  it("posts to the key's session route with the stored token and maps the config", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(SESSION_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const session = await startSession("http://api/", "pk_1", "tok-old");

    expect(fetchMock.mock.calls[0][0]).toBe("http://api/api/v1/widget/pk_1/session");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-old");
    expect(init.credentials).toBe("omit");
    expect(session).toEqual({
      token: "tok-new",
      expiresAt: "2026-10-25T00:00:00Z",
      config: {
        agentName: "Ava",
        title: "Ask us",
        greeting: "Hi there!",
        fallbackMessage: "Sorry, try again later.",
        brandColor: "#123abc",
        position: "left",
      },
    });
  });

  it("sends no Authorization header without a stored token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(SESSION_BODY));
    vi.stubGlobal("fetch", fetchMock);

    await startSession("http://api", "pk_1", null);

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("raises WidgetUnavailableError on a 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ error: { code: "not_found", message: "widget not found" } }, 404)),
    );
    await expect(startSession("http://api", "pk_1", null)).rejects.toBeInstanceOf(
      WidgetUnavailableError,
    );
  });

  it("raises a plain error on any other failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 500)));
    const failure = startSession("http://api", "pk_1", null);
    await expect(failure).rejects.toBeInstanceOf(Error);
    await expect(failure).rejects.not.toBeInstanceOf(WidgetUnavailableError);
  });
});

describe("request timeouts", () => {
  function timeoutError(): DOMException {
    return new DOMException("The operation timed out.", "TimeoutError");
  }

  it("gives up on a session request after 10 s, as unavailable-by-timeout", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const fetchMock = vi.fn().mockRejectedValue(timeoutError());
    vi.stubGlobal("fetch", fetchMock);

    await expect(startSession("http://api", "pk_1", null)).rejects.toBeInstanceOf(
      WidgetTimeoutError,
    );
    expect(timeout).toHaveBeenCalledWith(10_000);
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
    timeout.mockRestore();
  });

  it("gives up on a conversation request after 10 s", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const fetchMock = vi.fn().mockRejectedValue(timeoutError());
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadConversation("http://api", "tok")).rejects.toBeInstanceOf(WidgetTimeoutError);
    expect(timeout).toHaveBeenCalledWith(10_000);
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
    timeout.mockRestore();
  });

  it("lets any other network failure through unchanged", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    const failure = startSession("http://api", "pk_1", null);
    await expect(failure).rejects.toBeInstanceOf(TypeError);
  });
});

describe("loadConversation", () => {
  it("maps a resumed conversation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      json({
        conversation_id: "c1",
        messages: [
          { role: "user", text: "Hi" },
          { role: "assistant", text: "Hello" },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const conversation = await loadConversation("http://api", "tok");

    expect(fetchMock.mock.calls[0][0]).toBe("http://api/api/v1/widget/conversation");
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toEqual({
      Authorization: "Bearer tok",
    });
    expect(conversation).toEqual({
      conversationId: "c1",
      messages: [
        { role: "user", text: "Hi" },
        { role: "assistant", text: "Hello" },
      ],
    });
  });

  it("answers null when there is none", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(null)));
    expect(await loadConversation("http://api", "tok")).toBeNull();
  });

  it("raises WidgetUnavailableError on a 404", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 404)));
    await expect(loadConversation("http://api", "tok")).rejects.toBeInstanceOf(
      WidgetUnavailableError,
    );
  });
});

describe("streamWidgetChat", () => {
  function sseResponse(frames: unknown[]): Response {
    const body = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }

  it("streams projected events and sends the turn", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        { type: "message_start", conversation_id: "c1", message_id: "m1" },
        { type: "tool_call_start", calls: [{ name: "search_products" }] },
        { type: "text_delta", text: "Hi" },
        { type: "message_end" },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const events: WidgetEvent[] = [];

    await streamWidgetChat({
      apiUrl: "http://api",
      token: "tok",
      message: "hello",
      conversationId: null,
      onEvent: (e) => events.push(e),
    });

    expect(fetchMock.mock.calls[0][0]).toBe("http://api/api/v1/widget/chat/stream");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ message: "hello", conversation_id: null });
    expect(events.map((e) => e.type)).toEqual([
      "message_start",
      "tool_call_start",
      "text_delta",
      "message_end",
    ]);
  });

  it("turns a non-200 envelope into an error event", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          json({ error: { code: "widget_daily_cap", message: "not available" } }, 429),
        ),
    );
    const events: WidgetEvent[] = [];
    await streamWidgetChat({
      apiUrl: "http://api",
      token: "tok",
      message: "hi",
      conversationId: "c1",
      onEvent: (e) => events.push(e),
    });
    expect(events).toEqual([{ type: "error", code: "widget_daily_cap", message: "not available" }]);
  });

  it("falls back to internal_error for a non-JSON failure, like streamChat", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("<html>Bad gateway</html>", { status: 502 })),
    );
    const events: WidgetEvent[] = [];
    await streamWidgetChat({
      apiUrl: "http://api",
      token: "tok",
      message: "hi",
      conversationId: null,
      onEvent: (e) => events.push(e),
    });
    expect(events).toEqual([
      { type: "error", code: "internal_error", message: "Something went wrong. Please try again." },
    ]);
  });

  it("reports a network failure as network_error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    const events: WidgetEvent[] = [];
    await streamWidgetChat({
      apiUrl: "http://api",
      token: "tok",
      message: "hi",
      conversationId: null,
      onEvent: (e) => events.push(e),
    });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "error", code: "network_error" });
  });
});

describe("stored token helpers", () => {
  it("round-trip through localStorage under widget:{key}", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    });
    expect(readStoredToken("pk_1")).toBeNull();
    storeToken("pk_1", "tok");
    expect(store.get("widget:pk_1")).toBe("tok");
    expect(readStoredToken("pk_1")).toBe("tok");
  });

  it("survive a localStorage that throws", () => {
    const throwing = () => {
      throw new DOMException("blocked", "SecurityError");
    };
    vi.stubGlobal("localStorage", { getItem: throwing, setItem: throwing });
    expect(readStoredToken("pk_1")).toBeNull();
    expect(() => storeToken("pk_1", "tok")).not.toThrow();
  });

  it("survive no localStorage at all", () => {
    // The node environment has no localStorage global, like a sandboxed frame.
    expect(readStoredToken("pk_1")).toBeNull();
    expect(() => storeToken("pk_1", "tok")).not.toThrow();
  });
});
