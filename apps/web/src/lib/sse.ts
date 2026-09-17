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
// Next.js's tsconfig path mapping. `./auth` is a type-only import, so the
// React module it lives in is erased at compile time and never loaded here.
import { apiFetch } from "./api";
import type { TokenResponse } from "./auth";

export type ChatUsage = {
  input_tokens: number;
  output_tokens: number;
};

export type Citation = {
  chunk_id: string;
  document_id: string;
  /** Untrusted: this is the uploaded document's own title, never escaped by
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
};

export type SSEEvent =
  | { type: "message_start"; conversation_id: string; message_id: string }
  | { type: "citations"; citations: Citation[] }
  | { type: "text_delta"; text: string }
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

function toCitation(value: unknown): Citation | null {
  if (!isRecord(value)) return null;
  const { chunk_id, document_id, document_title, rank, score, excerpt } = value;
  if (
    typeof chunk_id !== "string" ||
    typeof document_id !== "string" ||
    typeof document_title !== "string" ||
    typeof rank !== "number" ||
    typeof score !== "number" ||
    typeof excerpt !== "string"
  ) {
    return null;
  }
  return { chunk_id, document_id, document_title, rank, score, excerpt };
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

/**
 * Parses a raw byte stream into `SSEEvent`s, buffering across chunk
 * boundaries and only emitting once a complete `\n\n`-terminated frame has
 * arrived. Malformed frames (bad JSON, or JSON that doesn't match a known
 * event shape) are silently skipped rather than throwing, so one
 * unrecognised frame (e.g. a future event type) doesn't take down the whole
 * stream.
 */
export async function* parseSSEStream(
  chunks: AsyncIterable<Uint8Array>,
): AsyncGenerator<SSEEvent, void, void> {
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
        let parsed: unknown;
        try {
          parsed = JSON.parse(data);
        } catch {
          parsed = null;
        }
        const event = parsed === null ? null : toSSEEvent(parsed);
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
    try {
      const event = toSSEEvent(JSON.parse(data));
      if (event !== null) yield event;
    } catch {
      // Incomplete trailing frame -- nothing to yield.
    }
  }
}

async function* readableStreamToIterable(
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
  const { agentId, message, conversationId, accessToken, apiUrl, onEvent, onAccessToken, signal } =
    params;

  const send = (token: string): Promise<Response> =>
    fetch(`${apiUrl}/api/v1/chat/stream`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        agent_id: agentId,
        message,
        conversation_id: conversationId ?? null,
      }),
      signal,
    });

  let response: Response;
  try {
    response = await send(accessToken);

    // GraphQL refreshes silently on a 401 via urql's `authExchange`, so a
    // dashboard tab left open past the access token's lifetime keeps
    // working -- except here, where a 401 used to render a red error bubble
    // with no retry at all. Same recovery, through the same single
    // `/api/v1/auth/refresh` call the exchange uses (the refresh token is an
    // httpOnly cookie, so `apiFetch` needs nothing from this module), and
    // ONE retry only: retrying a refresh that keeps coming back 401 is how
    // you build a refresh loop.
    if (response.status === 401) {
      let refreshed: string | null = null;
      try {
        const tokens = await apiFetch<TokenResponse>("/api/v1/auth/refresh", { method: "POST" });
        refreshed = tokens.access_token;
      } catch {
        // The refresh token itself is gone or invalid -- there is no session
        // left to salvage. Fall through and surface the original 401 exactly
        // as before, which is what moves the user back to /login.
        refreshed = null;
      }
      if (refreshed !== null) {
        onAccessToken?.(refreshed);
        response = await send(refreshed);
      }
    }
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
