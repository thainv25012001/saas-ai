import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/components/ui/cn";
import type { ChatUsage } from "@/lib/sse";

export type ChatMessageMeta = {
  model: string;
  usage: ChatUsage;
  costUsd: string | null;
  latencyMs: number;
};

export type ChatMessageError = {
  code: string;
  message: string;
};

export type ChatCitation = {
  chunkId: string;
  documentId: string;
  /** Untrusted -- the uploaded document's own title. See the module
   * docstring on `Citation` in `@/lib/sse` for why this must only ever be
   * rendered as JSX text, never through `dangerouslySetInnerHTML` or a
   * markdown renderer. */
  documentTitle: string;
  rank: number;
  score: number;
  /** Untrusted for the same reason as `documentTitle`. */
  excerpt: string;
};

export type ChatMessageData = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status: "streaming" | "done" | "error";
  error?: ChatMessageError;
  meta?: ChatMessageMeta;
  /** Arrives via the `citations` SSE event, before the first `text_delta` --
   * present (possibly empty) as soon as that event has been seen, `undefined`
   * before it (or for a user message, which never gets one). */
  citations?: ChatCitation[];
};

/** `cost_usd` is `null` whenever the model isn't in the pricing table (see
 * `docs/PHASE-2.md` §5) -- that's a real, honest state, not zero cost. */
function formatCost(costUsd: string | null): string {
  if (costUsd === null) return "not priced";
  const value = Number(costUsd);
  if (Number.isNaN(value)) return "not priced";
  return `$${value.toFixed(4)}`;
}

/** One fact per chip, each labelled, so the numbers can be read at a glance
 * instead of parsed out of a sentence. */
function MetaChips({ meta }: { meta: ChatMessageMeta }) {
  const chips = [
    { label: "Model", value: meta.model },
    { label: "Tokens", value: `${meta.usage.input_tokens} in / ${meta.usage.output_tokens} out` },
    { label: "Cost", value: formatCost(meta.costUsd) },
    { label: "Latency", value: `${meta.latencyMs}ms` },
  ];
  return (
    <div className="mt-2.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
      {chips.map((chip) => (
        <Badge key={chip.label} title={chip.label}>
          <span className="text-ink-subtle">{chip.label}</span>
          <span>{chip.value}</span>
        </Badge>
      ))}
    </div>
  );
}

/**
 * Numbered sources under the answer. Sorted by `rank` rather than trusted to
 * already be in order -- the array comes off the wire, and numbering by
 * position while the underlying data is unsorted would silently mislabel
 * every entry after the first.
 *
 * `document_title` and `excerpt` both originate in a file the customer
 * uploaded, and the backend deliberately does not escape them (see
 * `_citation_payload` in `apps/api/app/chat/service.py`) -- that is correct
 * at its layer, and escaping is this layer's job. Rendered here as plain
 * JSX text children only: no `dangerouslySetInnerHTML`, no markdown pass.
 * React escapes a text child by construction, so a title of `<script>` or
 * `<b>` prints as those literal characters instead of becoming markup.
 */
function Citations({ citations }: { citations: ChatCitation[] }) {
  if (citations.length === 0) return null;
  const sorted = [...citations].sort((a, b) => a.rank - b.rank);
  return (
    <div className="mt-2.5 border-t border-line pt-2.5">
      <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Sources</p>
      <ol className="mt-1.5 space-y-1.5">
        {sorted.map((citation) => (
          <li key={citation.chunkId} className="flex gap-1.5 text-xs text-ink-muted">
            <span className="shrink-0 font-medium text-ink-subtle">{citation.rank}.</span>
            <span className="min-w-0">
              <span className="font-medium text-ink">{citation.documentTitle}</span>
              <span> — {citation.excerpt}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function ChatMessage({ message }: { message: ChatMessageData }) {
  const isUser = message.role === "user";
  // An assistant turn stopped (via the Stop button, or a dropped connection)
  // before any token arrived has nothing to render in the text paragraph
  // below -- without this, that shows up as a near-invisible empty bubble.
  const stoppedWithNoText =
    !isUser && message.status === "done" && message.text === "" && !message.meta;

  return (
    <div className={cn("flex flex-col gap-1", isUser ? "items-end" : "items-start")}>
      <span className="px-1 text-xs font-medium text-ink-subtle">
        {isUser ? "You" : "Assistant"}
      </span>
      <div
        className={cn(
          "max-w-2xl rounded-card px-4 py-3 text-sm",
          isUser ? "bg-primary text-primary-ink" : "border border-line bg-surface text-ink",
        )}
      >
        {stoppedWithNoText ? (
          <p className="italic text-ink-subtle">Stopped before any response arrived.</p>
        ) : (
          <p className="whitespace-pre-wrap break-words">
            {message.text}
            {message.status === "streaming" && (
              <span aria-hidden className="ml-0.5 inline-block animate-pulse text-ink-subtle">
                ▍
              </span>
            )}
          </p>
        )}

        {message.status === "error" && message.error ? (
          <Alert tone="danger" className="mt-2.5">
            {message.error.message}
          </Alert>
        ) : null}

        {message.citations ? <Citations citations={message.citations} /> : null}

        {message.meta ? <MetaChips meta={message.meta} /> : null}
      </div>
    </div>
  );
}
