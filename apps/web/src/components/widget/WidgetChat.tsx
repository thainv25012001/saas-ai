"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnswerText } from "@/components/chat/AnswerText";
import { cn, focusRing } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";
import { Textarea } from "@/components/ui/Input";
import { readableTextOn } from "@/lib/contrast";
import { useStickToBottom } from "@/lib/use-stick-to-bottom";
import { LoadingState } from "@/components/ui/Spinner";
import {
  WidgetTimeoutError,
  WidgetUnavailableError,
  loadConversation,
  readStoredToken,
  startSession,
  storeToken,
  streamWidgetChat,
  toolLabel,
  type WidgetCitation,
  type WidgetConfig,
  type WidgetEvent,
  type WidgetSession,
} from "@/lib/widget-api";

/**
 * The chat inside the embeddable widget's iframe (spec
 * docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §6).
 *
 * It runs on a customer's site, for an anonymous visitor, so it is the
 * playground's transcript with everything internal taken out: no model, no
 * cost, no tool arguments -- the stream does not even carry them (§4.1).
 *
 * The one colour it does not take from `globals.css` is the business's own
 * `brand_color`, which is data: it is set once as the `--widget-brand`
 * custom property on the root and read through fixed `var()` classes, never
 * interpolated into a class name.
 */

export const UNAVAILABLE_MESSAGE = "This assistant is not available right now.";
const MAX_MESSAGE_LENGTH = 2000;
const COUNTER_FROM = 1800;
/** Stream error codes that mean "this widget is off", not "this turn failed". */
const UNAVAILABLE_CODES = new Set(["widget_daily_cap", "not_found"]);
const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

const BRAND_BG = "bg-[var(--widget-brand,var(--color-primary))]";
/** Black or white, whichever reads on the brand colour (`readableTextOn`). */
const BRAND_INK = "text-[var(--widget-brand-ink,var(--color-primary-ink))]";

type Bubble = {
  id: string;
  role: "user" | "assistant";
  text: string;
  streaming?: boolean;
  toolLabel?: string | null;
  citations?: WidgetCitation[];
};

type Phase = { kind: "loading" } | { kind: "unavailable" } | { kind: "ready"; session: WidgetSession };

function titleOf(config: WidgetConfig): string {
  return config.title || config.agentName || "Chat";
}

function openingBubbles(config: WidgetConfig): Bubble[] {
  return config.greeting ? [{ id: "greeting", role: "assistant", text: config.greeting }] : [];
}

/** A notice after whatever the assistant had already said, never instead of
 * it: a partial answer is still worth reading. */
function withNotice(text: string, notice: string): string {
  return text.trim() ? `${text}

${notice}` : notice;
}

function postToParent(message: Record<string, unknown>): void {
  // `*`: the embed page cannot know the host page's origin, and nothing it
  // posts is secret (public config, or "close").
  window.parent.postMessage(message, "*");
}

function sourcesLine(citations: WidgetCitation[]): string {
  const names = citations.map((c) =>
    c.page === null ? c.document_title : `${c.document_title} (p. ${c.page})`,
  );
  return `Sources: ${names.join(", ")}`;
}

function MessageBubble({ bubble }: { bubble: Bubble }) {
  if (bubble.role === "user") {
    return (
      <div
        className={cn(
          "ml-auto max-w-[85%] whitespace-pre-wrap break-words rounded-card px-3 py-2 text-sm",
          BRAND_BG,
          BRAND_INK,
        )}
      >
        {bubble.text}
      </div>
    );
  }
  const waiting = bubble.streaming && bubble.text === "" && !bubble.toolLabel;
  return (
    <div className="max-w-[85%] space-y-1">
      <div className="rounded-card border border-line bg-surface px-3 py-2 text-sm text-ink">
        {waiting ? <span className="text-ink-subtle">…</span> : <AnswerText text={bubble.text} />}
      </div>
      {bubble.toolLabel ? (
        <p role="status" className="text-xs text-ink-subtle">
          {bubble.toolLabel}
        </p>
      ) : null}
      {bubble.citations && bubble.citations.length > 0 ? (
        <p className="text-xs text-ink-muted">{sourcesLine(bubble.citations)}</p>
      ) : null}
    </div>
  );
}

const HEADER_BUTTON = cn(
  "inline-flex size-8 items-center justify-center rounded-control hover:bg-surface/15",
  focusRing,
  "focus-visible:ring-offset-1",
);

/** `Button`'s primary shape, painted with the brand instead of `bg-primary`:
 * layering a second background class over `Button`'s would resolve to
 * whichever Tailwind happens to emit later. */
const SEND_BUTTON = cn(
  "inline-flex items-center justify-center rounded-control px-3 py-1.5 text-sm font-medium",
  "transition-opacity hover:opacity-90",
  "disabled:cursor-not-allowed disabled:opacity-50",
  BRAND_BG,
  BRAND_INK,
  focusRing,
  "focus-visible:ring-offset-2",
);

function Header({
  title,
  onNewConversation,
}: {
  title: string;
  onNewConversation?: () => void;
}) {
  return (
    <header className={cn("flex items-center gap-1 px-4 py-3", BRAND_BG, BRAND_INK)}>
      <h1 className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</h1>
      {onNewConversation ? (
        <button
          type="button"
          aria-label="New conversation"
          title="New conversation"
          onClick={onNewConversation}
          className={HEADER_BUTTON}
        >
          <Icon name="plus" size="md" />
        </button>
      ) : null}
      <button
        type="button"
        aria-label="Close chat"
        title="Close chat"
        onClick={() => postToParent({ type: "close" })}
        className={HEADER_BUTTON}
      >
        <Icon name="close" size="md" />
      </button>
    </header>
  );
}

export function WidgetChat({ apiUrl, publicKey }: { apiUrl: string; publicKey: string }) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [messages, setMessages] = useState<Bubble[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const conversationId = useRef<string | null>(null);
  // Bumped by "New conversation", so a stream still arriving for the old
  // thread cannot write into the new one.
  const generation = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const nextId = useRef(0);
  // Follows new text only while the visitor is at the bottom, so scrolling up
  // to re-read an answer is not undone by every streamed token.
  const transcript = useStickToBottom(messages);
  const stickToBottom = transcript.stick;
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      let session: WidgetSession;
      try {
        session = await startSession(apiUrl, publicKey, readStoredToken(publicKey));
      } catch {
        if (!cancelled) setPhase({ kind: "unavailable" });
        return;
      }
      if (cancelled) return;
      storeToken(publicKey, session.token);

      let resumed: Awaited<ReturnType<typeof loadConversation>> = null;
      try {
        resumed = await loadConversation(apiUrl, session.token);
      } catch (err) {
        // A hung API (timeout) will not chat either, so it reads the same.
        if (err instanceof WidgetUnavailableError || err instanceof WidgetTimeoutError) {
          if (!cancelled) setPhase({ kind: "unavailable" });
          return;
        }
        // Anything else: start fresh rather than refuse to chat.
      }
      if (cancelled) return;

      conversationId.current = resumed?.conversationId ?? null;
      setMessages(
        resumed && resumed.messages.length > 0
          ? resumed.messages.map((m, i) => ({ id: `r${i}`, role: m.role, text: m.text }))
          : openingBubbles(session.config),
      );
      setPhase({ kind: "ready", session });
      postToParent({
        type: "ready",
        brand_color: session.config.brandColor,
        position: session.config.position,
        title: titleOf(session.config),
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [apiUrl, publicKey]);

  // The loader posts `open` each time the visitor reopens the panel.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (window.parent === window || event.source !== window.parent) return;
      const data: unknown = event.data;
      if (typeof data === "object" && data !== null && (data as { type?: unknown }).type === "open") {
        inputRef.current?.focus();
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => () => abort.current?.abort(), []);

  const session = phase.kind === "ready" ? phase.session : null;

  const newConversation = useCallback(() => {
    if (!session) return;
    generation.current += 1;
    abort.current?.abort();
    abort.current = null;
    conversationId.current = null;
    stickToBottom();
    setMessages(openingBubbles(session.config));
    setSending(false);
  }, [session, stickToBottom]);

  async function send() {
    const text = draft.trim();
    if (!session || sending || text.length === 0 || text.length > MAX_MESSAGE_LENGTH) return;
    stickToBottom();

    const turn = generation.current;
    const id = nextId.current++;
    const replyId = `a${id}`;
    const current = () => generation.current === turn;
    const update = (patch: (b: Bubble) => Bubble) =>
      setMessages((all) => all.map((b) => (b.id === replyId ? patch(b) : b)));

    setMessages((all) => [
      ...all,
      { id: `u${id}`, role: "user", text },
      { id: replyId, role: "assistant", text: "", streaming: true, toolLabel: null },
    ]);
    setDraft("");
    setSending(true);

    const controller = new AbortController();
    abort.current = controller;
    const { fallbackMessage } = session.config;
    // A resumed conversation can vanish (closed, or deleted) while the
    // visitor still holds its id; the API then answers `not_found`. That
    // turn is retried once as a new conversation rather than failed.
    let retryAsNew = false;
    let retried = false;
    // Anything thrown out of the stream (or out of `onEvent`) becomes the
    // fallback message, never an unhandled rejection.
    let failed = false;

    function handlerFor(sentConversationId: string | null) {
      return function onEvent(event: WidgetEvent) {
        if (!current()) return;
        switch (event.type) {
          case "message_start":
            conversationId.current = event.conversation_id;
            break;
          case "text_delta":
            update((b) => ({ ...b, text: b.text + event.text, toolLabel: null }));
            break;
          case "tool_call_start": {
            const last = event.calls[event.calls.length - 1];
            if (last) update((b) => ({ ...b, toolLabel: toolLabel(last.name) }));
            break;
          }
          case "citations":
            update((b) => ({ ...b, citations: [...(b.citations ?? []), ...event.citations] }));
            break;
          case "message_end":
            update((b) => ({ ...b, streaming: false, toolLabel: null }));
            break;
          case "error": {
            if (event.code === "not_found" && sentConversationId !== null && !retried) {
              retryAsNew = true;
              break;
            }
            const notice = UNAVAILABLE_CODES.has(event.code) ? UNAVAILABLE_MESSAGE : fallbackMessage;
            update((b) => ({
              ...b,
              text: withNotice(b.text, notice),
              streaming: false,
              toolLabel: null,
            }));
            break;
          }
        }
      };
    }

    const attempt = (sentConversationId: string | null) =>
      streamWidgetChat({
        apiUrl,
        token: session.token,
        message: text,
        conversationId: sentConversationId,
        onEvent: handlerFor(sentConversationId),
        signal: controller.signal,
      });

    try {
      await attempt(conversationId.current);
      if (retryAsNew && current()) {
        retried = true;
        conversationId.current = null;
        update((b) => ({ ...b, text: "", streaming: true, toolLabel: null, citations: undefined }));
        await attempt(null);
      }
    } catch {
      failed = true;
    } finally {
      if (current()) {
        // A stream that ended without an answer (a dropped connection) still
        // owes the visitor a reply.
        update((b) => ({
          ...b,
          text: b.text === "" || failed ? withNotice(b.text, fallbackMessage) : b.text,
          streaming: false,
          toolLabel: null,
        }));
        setSending(false);
        abort.current = null;
      }
    }
  }

  const brand = session && HEX_COLOR.test(session.config.brandColor) ? session.config.brandColor : null;
  const rootStyle = brand
    ? ({ "--widget-brand": brand, "--widget-brand-ink": readableTextOn(brand) } as React.CSSProperties)
    : undefined;

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-surface text-ink" style={rootStyle}>
      {phase.kind === "loading" ? (
        <>
          {/* The close button works before anything has loaded: on a small
              screen this frame covers the host page. */}
          <Header title="Chat" />
          <LoadingState label="Loading chat…" />
        </>
      ) : phase.kind === "unavailable" ? (
        <>
          <Header title="Chat" />
          <p className="px-4 py-6 text-sm text-ink-muted">{UNAVAILABLE_MESSAGE}</p>
        </>
      ) : (
        <>
          <Header title={titleOf(phase.session.config)} onNewConversation={newConversation} />
          <div
            ref={transcript.ref}
            onScroll={transcript.onScroll}
            role="log"
            aria-live="polite"
            aria-label="Conversation"
            className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-surface-muted px-4 py-4"
          >
            {messages.map((bubble) => (
              <MessageBubble key={bubble.id} bubble={bubble} />
            ))}
          </div>
          <form
            className="space-y-2 border-t border-line bg-surface p-3"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <Textarea
              ref={inputRef}
              aria-label="Message"
              placeholder="Type your message…"
              rows={2}
              maxLength={MAX_MESSAGE_LENGTH}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <div className="flex items-center justify-end gap-3">
              {draft.length > COUNTER_FROM ? (
                <span className="text-xs text-ink-subtle">{`${draft.length} / ${MAX_MESSAGE_LENGTH}`}</span>
              ) : null}
              <button
                type="submit"
                disabled={sending || draft.trim().length === 0}
                className={SEND_BUTTON}
              >
                Send
              </button>
            </div>
          </form>
        </>
      )}
    </div>
  );
}
