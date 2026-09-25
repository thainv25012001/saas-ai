/**
 * The embeddable widget's client for `/api/v1/widget` (spec
 * docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §4, §4.1).
 *
 * Nothing here touches the dashboard's auth: a widget request carries its own
 * visitor token, never a dashboard JWT, and sends no cookies
 * (`credentials: "omit"`) -- the embed page runs inside a customer's site.
 *
 * The stream carries the *projected* public events (§4.1), which are thinner
 * than the playground's and would all fail `toSSEEvent`, so they are parsed
 * with `parseSSEFrames` and this module's own `toWidgetEvent`.
 */

// Relative, like `sse.ts`, so vitest resolves it without the `@/` mapping.
import { parseErrorEnvelope } from "./api";
import { parseSSEFrames, readableStreamToIterable } from "./sse";

export type WidgetConfig = {
  agentName: string;
  title: string | null;
  greeting: string | null;
  fallbackMessage: string;
  brandColor: string;
  position: "right" | "left";
};

export type WidgetSession = { token: string; expiresAt: string; config: WidgetConfig };

export type WidgetCitation = { document_title: string; page: number | null };

export type WidgetEvent =
  | { type: "message_start"; conversation_id: string; message_id: string }
  | { type: "text_delta"; text: string }
  | { type: "tool_call_start"; calls: { name: string }[] }
  | { type: "citations"; citations: WidgetCitation[] }
  | { type: "message_end" }
  | { type: "error"; code: string; message: string };

export type WidgetMessage = { role: "user" | "assistant"; text: string };

export type WidgetConversation = { conversationId: string; messages: WidgetMessage[] };

/** The widget is off, the agent is not active, or the key is unknown -- the
 * API answers all of them with the same 404 and so does this. */
export class WidgetUnavailableError extends Error {
  constructor(message = "widget not found") {
    super(message);
    this.name = "WidgetUnavailableError";
  }
}

/** The API did not answer a session or conversation request in time. The
 * widget treats it like being unavailable: a hung request must never leave
 * the visitor looking at a spinner with no way forward. */
export class WidgetTimeoutError extends Error {
  constructor(message = "widget request timed out") {
    super(message);
    this.name = "WidgetTimeoutError";
  }
}

/** How long a session or conversation request may take, body included. */
export const REQUEST_TIMEOUT_MS = 10_000;

/** `fetch` with `REQUEST_TIMEOUT_MS`, turning the timeout into
 * `WidgetTimeoutError`. The signal also covers reading the body. */
async function timedFetch<T>(
  url: string,
  init: RequestInit,
  read: (response: Response) => Promise<T>,
): Promise<T> {
  try {
    const response = await fetch(url, { ...init, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
    return await read(response);
  } catch (err) {
    if (err instanceof DOMException && (err.name === "TimeoutError" || err.name === "AbortError")) {
      throw new WidgetTimeoutError();
    }
    throw err;
  }
}

/** A visitor-facing line per tool, shown while it runs. Never the tool's
 * arguments: the projection does not send them. */
export const TOOL_LABELS: Record<string, string> = {
  search_products: "Searching products…",
  get_product: "Looking up a product…",
  retrieve_knowledge: "Checking our information…",
  create_lead: "Saving your details…",
};

export function toolLabel(name: string): string {
  return Object.prototype.hasOwnProperty.call(TOOL_LABELS, name) ? TOOL_LABELS[name] : "Working…";
}

type Raw = Record<string, unknown>;

function isRecord(value: unknown): value is Raw {
  return typeof value === "object" && value !== null;
}

/** All-or-nothing, like `sse.ts`'s lists: a partial list would mislead. */
function mapAll<T>(value: unknown, one: (item: unknown) => T | null): T[] | null {
  if (!Array.isArray(value)) return null;
  const out: T[] = [];
  for (const item of value) {
    const mapped = one(item);
    if (mapped === null) return null;
    out.push(mapped);
  }
  return out;
}

function toCall(value: unknown): { name: string } | null {
  if (!isRecord(value) || typeof value.name !== "string") return null;
  return { name: value.name };
}

function toCitation(value: unknown): WidgetCitation | null {
  if (!isRecord(value)) return null;
  const { document_title, page } = value;
  if (typeof document_title !== "string") return null;
  if (typeof page !== "number" && page !== null) return null;
  return { document_title, page };
}

/** Narrows a parsed frame to a projected widget event, keeping only the
 * projected fields, or `null`. Never throws. */
export function toWidgetEvent(value: unknown): WidgetEvent | null {
  if (!isRecord(value) || typeof value.type !== "string") return null;
  switch (value.type) {
    case "message_start": {
      const { conversation_id, message_id } = value;
      if (typeof conversation_id !== "string" || typeof message_id !== "string") return null;
      return { type: "message_start", conversation_id, message_id };
    }
    case "text_delta":
      return typeof value.text === "string" ? { type: "text_delta", text: value.text } : null;
    case "tool_call_start": {
      const calls = mapAll(value.calls, toCall);
      return calls === null ? null : { type: "tool_call_start", calls };
    }
    case "citations": {
      const citations = mapAll(value.citations, toCitation);
      return citations === null ? null : { type: "citations", citations };
    }
    case "message_end":
      return { type: "message_end" };
    case "error": {
      const { code, message } = value;
      if (typeof code !== "string" || typeof message !== "string") return null;
      return { type: "error", code, message };
    }
    default:
      return null;
  }
}

function base(apiUrl: string): string {
  return `${apiUrl.replace(/\/+$/, "")}/api/v1/widget`;
}

function bearer(token: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function toConfig(value: unknown): WidgetConfig {
  const raw = isRecord(value) ? value : {};
  const text = (v: unknown): string | null => (typeof v === "string" ? v : null);
  return {
    agentName: text(raw.agent_name) ?? "",
    title: text(raw.title),
    greeting: text(raw.greeting),
    fallbackMessage: text(raw.fallback_message) ?? "Sorry, something went wrong. Please try again.",
    brandColor: text(raw.brand_color) ?? "",
    position: raw.position === "left" ? "left" : "right",
  };
}

/** Starts (or, with a stored token, resumes) the visitor's session. */
export async function startSession(
  apiUrl: string,
  publicKey: string,
  token: string | null,
): Promise<WidgetSession> {
  return timedFetch(
    `${base(apiUrl)}/${encodeURIComponent(publicKey)}/session`,
    { method: "POST", credentials: "omit", headers: { ...bearer(token) } },
    async (response) => {
      if (response.status === 404) throw new WidgetUnavailableError();
      if (!response.ok) throw new Error(`widget session failed: ${response.status}`);
      const body: unknown = await response.json();
      if (!isRecord(body) || typeof body.token !== "string") {
        throw new Error("widget session: malformed response");
      }
      return {
        token: body.token,
        expiresAt: typeof body.expires_at === "string" ? body.expires_at : "",
        config: toConfig(body.config),
      };
    },
  );
}

/** The visitor's latest open conversation, or `null`. */
export async function loadConversation(
  apiUrl: string,
  token: string,
): Promise<WidgetConversation | null> {
  return timedFetch(
    `${base(apiUrl)}/conversation`,
    { credentials: "omit", headers: bearer(token) },
    async (response) => {
      if (response.status === 404) throw new WidgetUnavailableError();
      if (!response.ok) throw new Error(`widget conversation failed: ${response.status}`);
      const body: unknown = await response.json();
      if (!isRecord(body) || typeof body.conversation_id !== "string") return null;
      const messages = Array.isArray(body.messages)
        ? body.messages.flatMap((m): WidgetMessage[] =>
            isRecord(m) &&
            (m.role === "user" || m.role === "assistant") &&
            typeof m.text === "string"
              ? [{ role: m.role, text: m.text }]
              : [],
          )
        : [];
      return { conversationId: body.conversation_id, messages };
    },
  );
}

export type StreamWidgetChatParams = {
  apiUrl: string;
  token: string;
  message: string;
  conversationId: string | null;
  onEvent: (event: WidgetEvent) => void;
  signal?: AbortSignal;
};

/** One visitor turn. A pre-stream refusal (429 daily cap, 404, 401) comes
 * back as a JSON envelope and is delivered as an `error` event, exactly like
 * `streamChat`, so the caller handles one shape. */
export async function streamWidgetChat(params: StreamWidgetChatParams): Promise<void> {
  const { apiUrl, token, message, conversationId, onEvent, signal } = params;

  let response: Response;
  try {
    response = await fetch(`${base(apiUrl)}/chat/stream`, {
      method: "POST",
      credentials: "omit",
      headers: { "Content-Type": "application/json", ...bearer(token) },
      body: JSON.stringify({ message, conversation_id: conversationId }),
      signal,
    });
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
    // The same fallback code `streamChat` uses for the same case.
    const { code, message: errorMessage } = await parseErrorEnvelope(response, "internal_error");
    onEvent({ type: "error", code, message: errorMessage });
    return;
  }

  if (!response.body) {
    onEvent({ type: "error", code: "internal_error", message: "The response had no body." });
    return;
  }

  try {
    for await (const event of parseSSEFrames(readableStreamToIterable(response.body), toWidgetEvent)) {
      onEvent(event);
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") return;
    onEvent({
      type: "error",
      code: "network_error",
      message: "The connection dropped. Please try again.",
    });
  }
}

function storageKey(publicKey: string): string {
  return `widget:${publicKey}`;
}

/**
 * The visitor token lives in `localStorage` so a returning visitor resumes
 * their conversation (spec §6). Every access is guarded: in a sandboxed or
 * third-party-storage-blocked frame, touching storage throws, and the widget
 * must still work -- it just does not resume.
 */
export function readStoredToken(publicKey: string): string | null {
  try {
    return localStorage.getItem(storageKey(publicKey));
  } catch {
    return null;
  }
}

export function storeToken(publicKey: string, token: string): void {
  try {
    localStorage.setItem(storageKey(publicKey), token);
  } catch {
    // Storage unavailable: this visit works, the next one starts fresh.
  }
}
