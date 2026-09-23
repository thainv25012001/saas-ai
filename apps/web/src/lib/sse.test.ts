import { afterEach, describe, expect, it, vi } from "vitest";
import { type SSEEvent, parseSSEStream, streamChat } from "./sse";

const encoder = new TextEncoder();

/** Turns a plain string into a single-chunk async iterable -- the "nice"
 * case every real network read is not guaranteed to give you. */
async function* chunksOf(...parts: string[]): AsyncGenerator<Uint8Array> {
  for (const part of parts) {
    yield encoder.encode(part);
  }
}

async function collect(chunks: AsyncIterable<Uint8Array>): Promise<SSEEvent[]> {
  const events: SSEEvent[] = [];
  for await (const event of parseSSEStream(chunks)) {
    events.push(event);
  }
  return events;
}

function frame(payload: unknown): string {
  return `data: ${JSON.stringify(payload)}\n\n`;
}

describe("parseSSEStream", () => {
  it("parses a single frame delivered in one chunk", async () => {
    const events = await collect(chunksOf(frame({ type: "text_delta", text: "hi" })));
    expect(events).toEqual([{ type: "text_delta", text: "hi" }]);
  });

  it("parses multiple frames delivered in one chunk", async () => {
    const events = await collect(
      chunksOf(
        frame({ type: "message_start", conversation_id: "c1", message_id: "m1" }) +
          frame({ type: "text_delta", text: "a" }) +
          frame({ type: "text_delta", text: "b" }),
      ),
    );
    expect(events).toEqual([
      { type: "message_start", conversation_id: "c1", message_id: "m1" },
      { type: "text_delta", text: "a" },
      { type: "text_delta", text: "b" },
    ]);
  });

  it("ignores heartbeat comment lines", async () => {
    const events = await collect(
      chunksOf(": ping\n\n" + frame({ type: "text_delta", text: "hi" }) + ": ping\n\n"),
    );
    expect(events).toEqual([{ type: "text_delta", text: "hi" }]);
  });

  it("reassembles a frame split mid-JSON across chunks", async () => {
    const whole = frame({ type: "text_delta", text: "hello world" });
    const splitPoint = whole.indexOf('"hello');
    const events = await collect(chunksOf(whole.slice(0, splitPoint), whole.slice(splitPoint)));
    expect(events).toEqual([{ type: "text_delta", text: "hello world" }]);
  });

  it("reassembles a frame split mid- the 'data:' prefix itself", async () => {
    const whole = frame({ type: "text_delta", text: "x" });
    // Split inside the literal characters "data:" (after "da").
    const events = await collect(chunksOf(whole.slice(0, 2), whole.slice(2)));
    expect(events).toEqual([{ type: "text_delta", text: "x" }]);
  });

  it("reassembles a frame split across the blank-line separator", async () => {
    const first = frame({ type: "text_delta", text: "one" });
    const second = frame({ type: "text_delta", text: "two" });
    // Split so the first "\n" of the "\n\n" separator ends chunk 1 and the
    // second "\n" starts chunk 2 -- the separator itself straddles the
    // boundary, not just the data around it.
    const combined = first + second;
    const separatorStart = first.length - 2; // index of the first \n of "\n\n"
    const events = await collect(
      chunksOf(combined.slice(0, separatorStart + 1), combined.slice(separatorStart + 1)),
    );
    expect(events).toEqual([
      { type: "text_delta", text: "one" },
      { type: "text_delta", text: "two" },
    ]);
  });

  it("splits a single frame across many single-byte chunks", async () => {
    const whole = frame({ type: "message_end", usage: { input_tokens: 1, output_tokens: 2 }, cost_usd: "0.01", latency_ms: 5, model: "fake-1", prompt_version_id: null });
    const bytes = encoder.encode(whole);
    async function* byteAtATime(): AsyncGenerator<Uint8Array> {
      for (const b of bytes) yield new Uint8Array([b]);
    }
    const events = await collect(byteAtATime());
    expect(events).toEqual([
      {
        type: "message_end",
        usage: { input_tokens: 1, output_tokens: 2 },
        cost_usd: "0.01",
        latency_ms: 5,
        model: "fake-1",
        prompt_version_id: null,
      },
    ]);
  });

  it("handles a multi-byte UTF-8 character split across chunk boundaries", async () => {
    const whole = frame({ type: "text_delta", text: "café — emoji 😀" });
    const bytes = encoder.encode(whole);
    // Cut in the middle of the buffer so at least one multi-byte sequence
    // is very likely split; TextDecoder's `{stream: true}` must carry the
    // partial sequence over rather than emit U+FFFD.
    const mid = Math.floor(bytes.length / 2);
    async function* twoChunks(): AsyncGenerator<Uint8Array> {
      yield bytes.slice(0, mid);
      yield bytes.slice(mid);
    }
    const events = await collect(twoChunks());
    expect(events).toEqual([{ type: "text_delta", text: "café — emoji 😀" }]);
  });

  it("treats cost_usd: null as null, not the string 'null'", async () => {
    const events = await collect(
      chunksOf(
        frame({
          type: "message_end",
          usage: { input_tokens: 10, output_tokens: 20 },
          cost_usd: null,
          latency_ms: 100,
          model: "gpt-4o-mini",
          prompt_version_id: null,
        }),
      ),
    );
    expect(events).toEqual([
      {
        type: "message_end",
        usage: { input_tokens: 10, output_tokens: 20 },
        cost_usd: null,
        latency_ms: 100,
        model: "gpt-4o-mini",
        prompt_version_id: null,
      },
    ]);
  });

  it("parses a citations event arriving between message_start and the first text_delta", async () => {
    const citation = {
      chunk_id: "c1",
      document_id: "doc1",
      product_id: null,
      document_title: "Pricing sheet.pdf",
      rank: 1,
      score: 0.87,
      excerpt: "Our starter plan is $19/mo.",
      page: 3,
    };
    const events = await collect(
      chunksOf(
        frame({ type: "message_start", conversation_id: "c1", message_id: "m1" }) +
          frame({ type: "citations", citations: [citation] }) +
          frame({ type: "text_delta", text: "Our" }),
      ),
    );
    expect(events).toEqual([
      { type: "message_start", conversation_id: "c1", message_id: "m1" },
      { type: "citations", citations: [citation] },
      { type: "text_delta", text: "Our" },
    ]);
  });

  it("parses a citations event with an empty list (an ungrounded answer)", async () => {
    const events = await collect(chunksOf(frame({ type: "citations", citations: [] })));
    expect(events).toEqual([{ type: "citations", citations: [] }]);
  });

  it("drops a citations event where one entry is missing a required field", async () => {
    // A partial list would misnumber the rest, since the UI displays `rank`
    // as-is -- dropping the whole event is safer than rendering it wrong.
    const events = await collect(
      chunksOf(
        frame({
          type: "citations",
          citations: [{ chunk_id: "c1", document_id: "doc1", rank: 1, score: 0.5, excerpt: "x" }],
        }) + frame({ type: "text_delta", text: "ok" }),
      ),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("accepts a citation with page: null, for a non-paginated source", async () => {
    const citation = {
      chunk_id: "c1",
      document_id: "doc1",
      product_id: null,
      document_title: "FAQ.md",
      rank: 1,
      score: 0.6,
      excerpt: "x",
      page: null,
    };
    const events = await collect(chunksOf(frame({ type: "citations", citations: [citation] })));
    expect(events).toEqual([{ type: "citations", citations: [citation] }]);
  });

  it("keeps document and product citations mixed in one event", async () => {
    // Final review I1: one `citations` event aggregates every tool in a
    // step, so a `retrieve_knowledge` chunk and a `search_products` row
    // arrive together. A product citation has null chunk/document ids; that
    // must not drop the event, and with it the document citation too.
    const documentCitation = {
      chunk_id: "c1",
      document_id: "doc1",
      product_id: null,
      document_title: "Pricing sheet.pdf",
      rank: 1,
      score: 0.87,
      excerpt: "Our starter plan is $19/mo.",
      page: 3,
    };
    const productCitation = {
      chunk_id: null,
      document_id: null,
      product_id: "prod1",
      document_title: "Aurora Sedan",
      rank: 1,
      score: 0.03,
      excerpt: "28499.00 USD · in_stock",
      page: null,
    };
    const events = await collect(
      chunksOf(frame({ type: "citations", citations: [documentCitation, productCitation] })),
    );
    expect(events).toEqual([
      { type: "citations", citations: [documentCitation, productCitation] },
    ]);
  });

  it("reads a citation with no product_id key as a document citation", async () => {
    const { product_id: _omitted, ...wire } = {
      chunk_id: "c1",
      document_id: "doc1",
      product_id: null,
      document_title: "FAQ.md",
      rank: 1,
      score: 0.6,
      excerpt: "x",
      page: null,
    };
    const events = await collect(chunksOf(frame({ type: "citations", citations: [wire] })));
    expect(events).toEqual([{ type: "citations", citations: [{ ...wire, product_id: null }] }]);
  });

  it("drops a citation that names neither a chunk nor a product", async () => {
    const events = await collect(
      chunksOf(
        frame({
          type: "citations",
          citations: [
            {
              chunk_id: null,
              document_id: null,
              product_id: null,
              document_title: "Nothing",
              rank: 1,
              score: 0,
              excerpt: "x",
              page: null,
            },
          ],
        }) + frame({ type: "text_delta", text: "ok" }),
      ),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("drops a citation whose product_id is neither a string nor null", async () => {
    const events = await collect(
      chunksOf(
        frame({
          type: "citations",
          citations: [
            {
              chunk_id: null,
              document_id: null,
              product_id: 7,
              document_title: "Aurora Sedan",
              rank: 1,
              score: 0,
              excerpt: "x",
              page: null,
            },
          ],
        }) + frame({ type: "text_delta", text: "ok" }),
      ),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("drops a citation whose page is neither a number nor null", async () => {
    const events = await collect(
      chunksOf(
        frame({
          type: "citations",
          citations: [
            {
              chunk_id: "c1",
              document_id: "doc1",
              document_title: "FAQ.md",
              rank: 1,
              score: 0.6,
              excerpt: "x",
              page: "3",
            },
          ],
        }) + frame({ type: "text_delta", text: "ok" }),
      ),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("parses a tool_call_start event with its calls' id, name and arguments", async () => {
    const call = { id: "call_1", name: "retrieve_knowledge", arguments: { query: "pricing" } };
    const events = await collect(chunksOf(frame({ type: "tool_call_start", calls: [call] })));
    expect(events).toEqual([{ type: "tool_call_start", calls: [call] }]);
  });

  it("drops a tool_call_start event where one call is missing arguments", async () => {
    // Same all-or-nothing reasoning as a malformed citations entry: a call
    // the UI cannot render correctly (no arguments to summarise) is worse
    // to show partially than to drop.
    const events = await collect(
      chunksOf(
        frame({ type: "tool_call_start", calls: [{ id: "call_1", name: "retrieve_knowledge" }] }) +
          frame({ type: "text_delta", text: "ok" }),
      ),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("parses a tool_call_end event, including a failed call's is_error flag", async () => {
    const results = [
      { tool_call_id: "call_1", tool_name: "retrieve_knowledge", result: "3 chunks found", is_error: false },
      { tool_call_id: "call_2", tool_name: "create_lead", result: "invalid arguments", is_error: true },
    ];
    const events = await collect(chunksOf(frame({ type: "tool_call_end", results })));
    expect(events).toEqual([{ type: "tool_call_end", results }]);
  });

  it("drops a tool_call_end event where one result is missing is_error", async () => {
    const events = await collect(
      chunksOf(
        frame({
          type: "tool_call_end",
          results: [{ tool_call_id: "call_1", tool_name: "retrieve_knowledge", result: "ok" }],
        }) + frame({ type: "text_delta", text: "ok" }),
      ),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("parses an error event and preserves its code and message", async () => {
    const events = await collect(
      chunksOf(frame({ type: "error", code: "llm_rate_limited", message: "Too many requests" })),
    );
    expect(events).toEqual([{ type: "error", code: "llm_rate_limited", message: "Too many requests" }]);
  });

  it("skips a malformed frame instead of throwing", async () => {
    const events = await collect(chunksOf("data: {not json\n\n" + frame({ type: "text_delta", text: "ok" })));
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("skips an unrecognised event type instead of throwing", async () => {
    const events = await collect(
      chunksOf(frame({ type: "future_event", whatever: 1 }) + frame({ type: "text_delta", text: "ok" })),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("still recovers a final frame whose JSON is complete but whose trailing blank line never arrived", async () => {
    // A well-behaved server always terminates a frame with "\n\n", but a
    // connection can close the instant after the last byte of valid JSON
    // and before the separator. Since the payload itself is well-formed,
    // this is treated as a real event rather than discarded -- the data
    // arrived, only the terminator did not.
    const events = await collect(chunksOf('data: {"type":"text_delta","text":"partial"}'));
    expect(events).toEqual([{ type: "text_delta", text: "partial" }]);
  });

  it("discards a genuinely truncated final frame (incomplete JSON)", async () => {
    const events = await collect(chunksOf('data: {"type":"text_delta","text":"cut off'));
    expect(events).toEqual([]);
  });
});

describe("streamChat token refresh", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const SSE_BODY =
    'data: {"type": "message_start", "conversation_id": "c1", "message_id": "m1"}\n\n' +
    'data: {"type": "text_delta", "text": "hi"}\n\n';

  const unauthorized = () =>
    new Response(JSON.stringify({ error: { code: "unauthenticated", message: "expired" } }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });

  /** Records every request and answers each one from `responses`, in order. */
  function stubFetch(responses: (() => Response)[]) {
    const calls: { url: string; init: RequestInit | undefined }[] = [];
    let index = 0;
    vi.stubGlobal("fetch", async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      const next = responses[index];
      index += 1;
      if (!next) throw new Error(`unexpected extra fetch call to ${String(url)}`);
      return next();
    });
    return calls;
  }

  function run(events: SSEEvent[], tokens: string[]) {
    return streamChat({
      agentId: "a1",
      message: "hello",
      conversationId: null,
      accessToken: "stale-token",
      apiUrl: "http://api.test",
      onEvent: (event) => events.push(event),
      onAccessToken: (token) => tokens.push(token),
    });
  }

  it("refreshes once and retries the stream after a 401", async () => {
    // GraphQL already recovers from an expired access token silently via
    // urql's authExchange. Without this, a playground tab left open past the
    // token's lifetime renders a red error bubble on its next send while the
    // rest of the dashboard keeps working.
    const calls = stubFetch([
      unauthorized,
      () => new Response(JSON.stringify({ access_token: "fresh-token", expires_in: 900 })),
      () => new Response(SSE_BODY, { status: 200 }),
    ]);
    const events: SSEEvent[] = [];
    const tokens: string[] = [];

    await run(events, tokens);

    // The two halves of the split in `auth-proxy.ts`, side by side: the
    // stream goes straight to the API on a Bearer token, while the refresh is
    // relative so it travels through this app's origin and keeps its cookie
    // first-party.
    expect(calls.map((c) => c.url)).toEqual([
      "http://api.test/api/v1/chat/stream",
      "/api/v1/auth/refresh",
      "http://api.test/api/v1/chat/stream",
    ]);
    // The retry carries the rotated token, not the stale one.
    const retryHeaders = calls[2].init?.headers as Record<string, string>;
    expect(retryHeaders.Authorization).toBe("Bearer fresh-token");
    // ...and it is handed back so React state holds the fresh one too.
    expect(tokens).toEqual(["fresh-token"]);
    expect(events.map((e) => e.type)).toEqual(["message_start", "text_delta"]);
  });

  it("retries at most once, so a still-401 retry cannot loop", async () => {
    const calls = stubFetch([
      unauthorized,
      () => new Response(JSON.stringify({ access_token: "fresh-token", expires_in: 900 })),
      unauthorized,
    ]);
    const events: SSEEvent[] = [];

    await run(events, []);

    expect(calls).toHaveLength(3);
    expect(events).toEqual([{ type: "error", code: "unauthenticated", message: "expired" }]);
  });

  it("surfaces the original 401 when the refresh itself fails", async () => {
    const calls = stubFetch([unauthorized, unauthorized]);
    const events: SSEEvent[] = [];
    const tokens: string[] = [];

    await run(events, tokens);

    // No second stream attempt: there is no session left to salvage.
    expect(calls).toHaveLength(2);
    expect(tokens).toEqual([]);
    expect(events).toEqual([{ type: "error", code: "unauthenticated", message: "expired" }]);
  });
});

describe("streamChat request body", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const ok = () =>
    new Response('data: {"type": "text_delta", "text": "hi"}\n\n', {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  function stubOnce() {
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", async (_url: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return ok();
    });
    return bodies;
  }

  const base = {
    agentId: "a1",
    message: "hello",
    conversationId: null,
    accessToken: "token",
    apiUrl: "http://api.test",
    onEvent: () => {},
  };

  it("omits provider and model when no override is given", async () => {
    // The API treats a present-but-empty model as a bad request, and every
    // non-playground caller sends no override at all -- so "no override" has
    // to mean absent keys, not nulls.
    const bodies = stubOnce();

    await streamChat(base);

    expect(bodies[0]).toEqual({ agent_id: "a1", message: "hello", conversation_id: null });
  });

  it("sends an override as snake_case provider and model", async () => {
    const bodies = stubOnce();

    await streamChat({ ...base, override: { provider: "anthropic", model: "claude-sonnet-5" } });

    expect(bodies[0]).toEqual({
      agent_id: "a1",
      message: "hello",
      conversation_id: null,
      provider: "anthropic",
      model: "claude-sonnet-5",
    });
  });

  it("carries the override through a silent token refresh", async () => {
    // The retry after a 401 rebuilds the body from scratch. A version that
    // rebuilt it without the override would answer the retried turn on the
    // agent's model while the header still claimed the overridden one.
    const bodies: Record<string, unknown>[] = [];
    let call = 0;
    vi.stubGlobal("fetch", async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).endsWith("/api/v1/auth/refresh")) {
        return new Response(JSON.stringify({ access_token: "fresh" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      call += 1;
      if (call === 1) {
        return new Response(JSON.stringify({ error: { code: "unauthenticated", message: "x" } }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }
      return ok();
    });

    await streamChat({ ...base, override: { provider: "openai", model: "gpt-4o" } });

    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toMatchObject({ provider: "openai", model: "gpt-4o" });
  });
});
