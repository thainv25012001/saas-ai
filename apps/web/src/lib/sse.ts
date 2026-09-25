/**
 * Streaming transport for `POST /api/v1/chat/stream` -- see `docs/PHASE-2.md`
 * §4 for the event envelope this parses.
 *
 * `EventSource` cannot be used here: it is GET-only and cannot send an
 * `Authorization` header, and this route requires a bearer token. So this
 * module drives the request with `fetch` + a `ReadableStream` body reader
 * and parses Server-Sent Events "by hand".
 *
 * The parsing here is deliberately split out as a pure function,
 * `parseSSEStream`, over an `AsyncIterable<Uint8Array>` -- not tied to
 * `fetch` or `ReadableStream` -- so it can be exercised directly against
 * hand-built chunk sequences (see `sse.test.ts`) without a running API or a
 * real network stream. The one property that matters and is easy to get
 * wrong: a `data:` line, or the blank line separating two frames, can land
 * on either side of a chunk boundary. This buffers across chunks and only
 * parses once a full `\n\n`-terminated frame is available.
 */

// Relative, not the `@/` alias the rest of the app uses: this module is
// exercised directly by `sse.test.ts` under vitest, which resolves without
// Next.js's tsconfig path mapping.
import { fetchWithRefresh } from "./api";

export type ChatUsage = {
  input_tokens: number;
  output_tokens: number;
};

export type Citation = {
  /** `null` for a product citation (`search_products`/`get_product`), which
   * names a product row instead of a chunk -- see `product_id`. */
  chunk_id: string | null;
  document_id: string | null;
  /** Set for a product citation, `null` for a document one. Optional on the
   * wire only for tolerance of an older server that never sent the key;
   * normalised to `null` when absent. */
  product_id: string | null;
  /** Untrusted: this is the uploaded document's own title (or, for a product
   * citation, the product's name), never escaped by
   * the server (see `_citation_payload` in `apps/api/app/chat/service.py`).
   * Rendering it must go through JSX text interpolation only -- never
   * `dangerouslySetInnerHTML` or a markdown pass -- so React's own escaping
   * is what keeps a title like `<script>` from doing anything but printing
   * itself. See `ChatMessage.tsx`. */
  document_title: string;
  /** 1-based rank among this turn's retrieved chunks. */
  rank: number;
  score: number;
  /** Untrusted for the same reason as `document_title` -- a short preview
   * of the chunk's own content. */
  excerpt: string;
  /** 1-based page number for a PDF-sourced chunk; `null` for a source with
   * no page concept (plain text, Markdown, HTML) -- see `CitationPayload`
   * in `apps/api/app/chat/service.py`. */
  page: number | null;
};

export type ToolCall = {
  id: string;
  name: string;
  /** Untrusted: the model's own tool-call arguments, per `ChatToolCall`'s
   * docstring in `apps/api/app/chat/service.py`. Bounded by the tool's
   * `args_model` schema, but the *values* inside it can still be arbitrary
   * text a visitor typed (a `create_lead` call quotes their own
   * name/email/interest back). Rendering it must go through JSX text
   * interpolation only -- never `dangerouslySetInnerHTML` or a markdown
   * pass -- see `ToolCall.tsx`. */
  arguments: Record<string, unknown>;
};

export type ToolCallResult = {
  tool_call_id: string;
  tool_name: string;
  /** Untrusted for the same reason `Citation.excerpt` is: an excerpt of
   * whatever the tool returned, which for `retrieve_knowledge` is drawn
   * from an uploaded document's own content. */
  result: string;
  is_error: boolean;
};

export type SSEEvent =
  | { type: "message_start"; conversation_id: string; message_id: string }
  | { type: "citations"; citations: Citation[] }
  | { type: "text_delta"; text: string }
  | { type: "tool_call_start"; calls: ToolCall[] }
  | { type: "tool_call_end"; results: ToolCallResult[] }
  | {
      type: "message_end";
      usage: ChatUsage;
      cost_usd: string | null;
      latency_ms: number;
      model: string;
      prompt_version_id: string | null;
    }
  | { type: "error"; code: string; message: string };

type RawEvent = Record<string, unknown>;

function isRecord(value: unknown): value is RawEvent {
  return typeof value === "object" && value !== null;
}

function toChatUsage(value: unknown): ChatUsage | null {
  if (!isRecord(value)) return null;
  const { input_tokens, output_tokens } = value;
  if (typeof input_tokens !== "number" || typeof output_tokens !== "number") return null;
  return { input_tokens, output_tokens };
}

function isStringOrNull(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function toCitation(value: unknown): Citation | null {
  if (!isRecord(value)) return null;
  const { chunk_id, document_id, document_title, rank, score, excerpt, page } = value;
  const product_id = value.product_id === undefined ? null : value.product_id;
  if (
    // Each nullable, but a citation must still name *something* it came
    // from: a chunk or a product. One with neither is malformed.
    !isStringOrNull(chunk_id) ||
    !isStringOrNull(document_id) ||
    !isStringOrNull(product_id) ||
    (chunk_id === null && product_id === null) ||
    typeof document_title !== "string" ||
    typeof rank !== "number" ||
    typeof score !== "number" ||
    typeof excerpt !== "string" ||
    (typeof page !== "number" && page !== null)
  ) {
    return null;
  }
  return { chunk_id, document_id, product_id, document_title, rank, score, excerpt, page };
}

/** `null` if any single citation is malformed -- a partial citations list
 * would be worse than none, since the UI numbers them by array position and
 * a silently-dropped entry would misnumber the rest. */
function toCitations(value: unknown): Citation[] | null {
  if (!Array.isArray(value)) return null;
  const citations: Citation[] = [];
  for (const item of value) {
    const citation = toCitation(item);
    if (citation === null) return null;
    citations.push(citation);
  }
  return citations;
}

function toToolCall(value: unknown): ToolCall | null {
  if (!isRecord(value)) return null;
  // Destructured as `args`, not `arguments`: an ES module runs in strict
  // mode, where `arguments` is a reserved binding name and this would be a
  // SyntaxError, not just a shadowing warning.
  const { id, name, arguments: args } = value;
  if (typeof id !== "string" || typeof name !== "string" || !isRecord(args)) return null;
  return { id, name, arguments: args };
}

/** Same all-or-nothing shape as `toCitations`, for the same reason: a tool
 * call the UI cannot show correctly is worse to show partially than not at
 * all. */
function toToolCalls(value: unknown): ToolCall[] | null {
  if (!Array.isArray(value)) return null;
  const calls: ToolCall[] = [];
  for (const item of value) {
    const call = toToolCall(item);
    if (call === null) return null;
    calls.push(call);
  }
  return calls;
}

function toToolCallResult(value: unknown): ToolCallResult | null {
  if (!isRecord(value)) return null;
  const { tool_call_id, tool_name, result, is_error } = value;
  if (
    typeof tool_call_id !== "string" ||
    typeof tool_name !== "string" ||
    typeof result !== "string" ||
    typeof is_error !== "boolean"
  ) {
    return null;
  }
  return { tool_call_id, tool_name, result, is_error };
}

function toToolCallResults(value: unknown): ToolCallResult[] | null {
  if (!Array.isArray(value)) return null;
  const results: ToolCallResult[] = [];
  for (const item of value) {
    const result = toToolCallResult(item);
    if (result === null) return null;
    results.push(result);
  }
  return results;
}

/** Narrows a parsed JSON value into a known `SSEEvent`, or `null` if it does
 * not match any known shape (a forward-compatible unknown event type, or a
 * malformed payload). Never throws. */
function toSSEEvent(value: unknown): SSEEvent | null {
  if (!isRecord(value) || typeof value.type !== "string") return null;

  switch (value.type) {
    case "message_start": {
      const { conversation_id, message_id } = value;
      if (typeof conversation_id !== "string" || typeof message_id !== "string") return null;
      return { type: "message_start", conversation_id, message_id };
    }
    case "citations": {
      const citations = toCitations(value.citations);
      if (citations === null) return null;
      return { type: "citations", citations };
    }
    case "text_delta": {
      const { text } = value;
      if (typeof text !== "string") return null;
      return { type: "text_delta", text };
    }
    case "tool_call_start": {
      const calls = toToolCalls(value.calls);
      if (calls === null) return null;
      return { type: "tool_call_start", calls };
    }
    case "tool_call_end": {
      const results = toToolCallResults(value.results);
      if (results === null) return null;
      return { type: "tool_call_end", results };
    }
    case "message_end": {
      const usage = toChatUsage(value.usage);
      const { model, latency_ms, cost_usd, prompt_version_id } = value;
      if (usage === null || typeof model !== "string" || typeof latency_ms !== "number") {
        return null;
      }
      return {
        type: "message_end",
        usage,
        cost_usd: typeof cost_usd === "string" ? cost_usd : null,
        latency_ms,
        model,
        prompt_version_id: typeof prompt_version_id === "string" ? prompt_version_id : null,
      };
    }
    case "error": {
      const { code, message } = value;
      if (typeof code !== "string" || typeof message !== "string") return null;
      return { type: "error", code, message };
    }
    default:
      return null;
  }
}

/** Extracts the `data:` payload (possibly multi-line, per the SSE spec) from
 * one frame -- the text between two `\n\n` separators -- ignoring comment
 * lines (`:`-prefixed, used here for the `: ping` heartbeat) and any other
 * SSE field this backend never sends (`event:`, `id:`, `retry:`). */
function extractData(frame: string): string | null {
  const dataLines: string[] = [];
  for (const rawLine of frame.split("\n")) {
    // A trailing \r survives if the server (or a proxy) uses CRLF framing.
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line.length === 0) continue;
    if (line.startsWith(":")) continue; // comment / heartbeat
    if (!line.startsWith("data:")) continue;
    // Per the SSE spec, exactly one leading space after the colon is
    // stripped if present -- the rest of the line is verbatim.
    const value = line.slice(5);
    dataLines.push(value.startsWith(" ") ? value.slice(1) : value);
  }
  if (dataLines.length === 0) return null;
  return dataLines.join("\n");
}

/** One frame's `data:` payload, parsed and narrowed by `toEvent`; `null` for
 * bad JSON or a shape `toEvent` does not recognise. */
function parseFrame<E>(data: string, toEvent: (value: unknown) => E | null): E | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }
  return parsed === null ? null : toEvent(parsed);
}

/**
 * Parses a raw byte stream into events, buffering across chunk boundaries
 * and only emitting once a complete `\n\n`-terminated frame has arrived.
 * `toEvent` decides what a frame's JSON may become: the playground passes
 * `toSSEEvent` (via `parseSSEStream`), the widget its own `toWidgetEvent`,
 * because the widget's projected events would fail the playground's full
 * shapes. Malformed frames (bad JSON, or JSON `toEvent` rejects) are
 * silently skipped rather than throwing, so one unrecognised frame (e.g. a
 * future event type) doesn't take down the whole stream.
 */
export async function* parseSSEFrames<E>(
  chunks: AsyncIterable<Uint8Array>,
  toEvent: (value: unknown) => E | null,
): AsyncGenerator<E, void, void> {
  // `{ stream: true }` below keeps partial multi-byte UTF-8 sequences that
  // land on a chunk boundary in the decoder's internal state rather than
  // emitting U+FFFD for them -- this decoder instance must live for the
  // whole stream, not be recreated per chunk.
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  for await (const chunk of chunks) {
    buffer += decoder.decode(chunk, { stream: true });

    let separatorIndex = buffer.indexOf("\n\n");
    while (separatorIndex !== -1) {
      const frame = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);

      const data = extractData(frame);
      if (data !== null) {
        const event = parseFrame(data, toEvent);
        if (event !== null) yield event;
      }

      separatorIndex = buffer.indexOf("\n\n");
    }
  }

  // Flush any residual decoder state (a final, complete multi-byte
  // sequence) and attempt to parse whatever is left as a last frame that
  // never got a trailing blank line -- a well-behaved server always
  // terminates frames with "\n\n", so in practice this only matters for a
  // connection that ends mid-frame, which we treat as "nothing more to
  // report" rather than an error.
  buffer += decoder.decode();
  const data = extractData(buffer);
  if (data !== null) {
    const event = parseFrame(data, toEvent);
    if (event !== null) yield event;
  }
}

/** The playground's stream: `parseSSEFrames` over the full event shapes. */
export function parseSSEStream(
  chunks: AsyncIterable<Uint8Array>,
): AsyncGenerator<SSEEvent, void, void> {
  return parseSSEFrames(chunks, toSSEEvent);
}

export async function* readableStreamToIterable(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<Uint8Array, void, void> {
  const reader = stream.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      if (value) yield value;
    }
  } finally {
    reader.releaseLock();
  }
}

export type StreamChatParams = {
  agentId: string;
  message: string;
  /** `null`/`undefined` starts a new conversation; pass the `conversation_id`
   * from a prior `message_start` to continue it. */
  conversationId?: string | null;
  accessToken: string;
  apiUrl: string;
  onEvent: (event: SSEEvent) => void;
  /** Called with a rotated access token after a silent refresh, so the
   * caller can push it into React state exactly as urql's `authExchange`
   * does -- otherwise the fresh token dies with this call and the next send
   * repeats the whole refresh. */
  onAccessToken?: (token: string) => void;
  /** Answer this one turn with a provider/model other than the agent's
   * configured pair, without changing the agent -- the playground's model
   * picker. Absent means "the agent's own", and the fields are then left out
   * of the body entirely rather than sent as nulls: the API rejects a
   * present-but-empty `model`, and every other caller sends no override at
   * all. Build it with `overrideFields` from `./model-selection`. */
  override?: { provider?: string; model?: string };
  signal?: AbortSignal;
};

/**
 * Drives one chat turn against `POST /api/v1/chat/stream` and delivers each
 * parsed event to `onEvent` as it arrives.
 *
 * Errors raised *before* the stream starts (unknown agent, misconfigured
 * provider, etc.) come back as a normal JSON envelope on a non-200 response
 * -- not as an SSE event, since the server has not committed a 200 status
 * line yet. This function normalizes that case into the same `SSEEvent`
 * shape (`type: "error"`) so callers only ever have to handle one shape.
 */
export async function streamChat(params: StreamChatParams): Promise<void> {
  const {
    agentId,
    message,
    conversationId,
    accessToken,
    apiUrl,
    onEvent,
    onAccessToken,
    override,
    signal,
  } = params;

  // Built once, outside `send`, so the retry after a silent refresh below
  // sends the same turn -- an override rebuilt only on the first attempt
  // would answer the retried turn on a different model than the one the
  // caller asked for.
  const body = JSON.stringify({
    agent_id: agentId,
    message,
    conversation_id: conversationId ?? null,
    ...override,
  });

  const send = (token: string): Promise<Response> =>
    fetch(`${apiUrl}/api/v1/chat/stream`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body,
      signal,
    });

  let response: Response;
  try {
    // A 401 here used to render a red error bubble with no retry at all, in a
    // dashboard where GraphQL recovers silently. `fetchWithRefresh` owns that
    // recovery now, for this stream and for document uploads both.
    response = await fetchWithRefresh(send, accessToken, onAccessToken);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") return;
    onEvent({
      type: "error",
      code: "network_error",
      message: "Could not reach the server. Please try again.",
    });
    return;
  }

  if (!response.ok) {
    // Pre-stream failure: a plain JSON error envelope, per
    // `docs/PHASE-2.md` §4 and `app/api/chat.py`'s module docstring.
    const body: unknown = await response.json().catch(() => null);
    const error = isRecord(body) && isRecord(body.error) ? body.error : null;
    const code = typeof error?.code === "string" ? error.code : "internal_error";
    const errMessage =
      typeof error?.message === "string" ? error.message : "Something went wrong. Please try again.";
    onEvent({ type: "error", code, message: errMessage });
    return;
  }

  if (!response.body) {
    onEvent({
      type: "error",
      code: "internal_error",
      message: "The server response had no body to stream.",
    });
    return;
  }

  try {
    for await (const event of parseSSEStream(readableStreamToIterable(response.body))) {
      onEvent(event);
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") return;
    throw err;
  }
}
