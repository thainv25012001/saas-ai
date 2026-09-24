import { AnswerText } from "@/components/chat/AnswerText";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/components/ui/cn";
import { ToolCall, type ToolCallData } from "@/components/chat/ToolCall";
import type { ChatUsage } from "@/lib/sse";

export type { ToolCallData } from "@/components/chat/ToolCall";

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
  /** `null` for a product citation, and for a stored citation whose chunk
   * has since been deleted. */
  chunkId: string | null;
  documentId: string | null;
  /** Set for a product citation (`search_products`/`get_product`). */
  productId: string | null;
  /** Untrusted -- the uploaded document's own title, or a product's name.
   * See the module
   * docstring on `Citation` in `@/lib/sse` for why this must only ever be
   * rendered as JSX text, never through `dangerouslySetInnerHTML` or a
   * markdown renderer. */
  documentTitle: string;
  rank: number;
  score: number;
  /** Untrusted for the same reason as `documentTitle`. */
  excerpt: string;
  /** 1-based page number for a PDF-sourced chunk, `null` for a source with
   * no page concept (plain text, Markdown, HTML). Most corpora are not
   * PDFs, so the no-page case must look deliberate, not like a missing
   * value -- see `Citations` below. */
  page: number | null;
};

export type ChatMessageData = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status: "streaming" | "done" | "error";
  error?: ChatMessageError;
  meta?: ChatMessageMeta;
  /** Arrives via the `citations` SSE event. Phase 3 guaranteed this before
   * the first `text_delta`; Phase 4 makes retrieval a tool the model can
   * call after already speaking, so citations may now land mid-stream, or
   * even after the visible answer, and this must not be read as "arrives
   * early" any more. Present (possibly empty) as soon as any `citations`
   * event has been seen, `undefined` before that (or for a user message,
   * which never gets one). */
  citations?: ChatCitation[];
  /** Every `tool_call_start`/`tool_call_end` pair seen this turn, in the
   * order the calls started -- `undefined` for a turn (or a user message)
   * that never called one. */
  toolCalls?: ToolCallData[];
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
        {sorted.map((citation, index) => (
          // Index in the key: neither id is guaranteed -- a product citation
          // has no chunk, a stored one may have lost both to a deletion --
          // and one step's calls can cite the same row twice. The list is
          // re-sorted from scratch on every render, never reordered in place.
          <li
            key={`${citation.chunkId ?? citation.productId ?? "source"}-${index}`}
            className="flex gap-1.5 text-xs text-ink-muted"
          >
            <span className="shrink-0 font-medium text-ink-subtle">{citation.rank}.</span>
            <span className="min-w-0">
              {citation.productId !== null ? (
                <span className="text-ink-subtle">Product: </span>
              ) : null}
              <span className="font-medium text-ink">{citation.documentTitle}</span>
              {/* Most corpora are not PDFs, so a missing page must read as
                * deliberate (nothing rendered) rather than a blank where a
                * number was expected -- only ever shown when the source
                * actually has one. */}
              {citation.page !== null ? (
                <span className="text-ink-subtle">, page {citation.page}</span>
              ) : null}
              <span> — {citation.excerpt}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** The turn's tool-call trace, one card per call in the order it started.
 * Kept as its own section (like `Citations`) rather than spliced into the
 * text at the token position it happened: the SSE stream does not report
 * where within the answer's text a call fell, only that it did, and a
 * section that is honest about that is better than one that guesses. */
function ToolCalls({ toolCalls }: { toolCalls: ToolCallData[] }) {
  if (toolCalls.length === 0) return null;
  return (
    <div className="mt-2.5 space-y-1.5 border-t border-line pt-2.5">
      {toolCalls.map((call) => (
        <ToolCall key={call.id} data={call} />
      ))}
    </div>
  );
}

/** `step_limit_reached` is the one `error` code with its own copy and tone:
 * the agent stopped at a configured boundary having possibly already said
 * something and possibly already called tools, which is a real outcome a
 * user can act on (ask a narrower question), not a crash to apologise for.
 * Every other code keeps the generic danger treatment. */
function TurnError({ error }: { error: ChatMessageError }) {
  if (error.code === "step_limit_reached") {
    return (
      <Alert tone="warn" title="Reached its step limit" className="mt-2.5">
        {error.message}
      </Alert>
    );
  }
  return (
    <Alert tone="danger" className="mt-2.5">
      {error.message}
    </Alert>
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
        ) : isUser ? (
          <p className="whitespace-pre-wrap break-words">{message.text}</p>
        ) : (
          <>
            {/* Mid-stream the text can end in half a construct (a lone `**`);
              * that shows as literal text until the rest arrives. */}
            <AnswerText text={message.text} />
            {message.status === "streaming" && (
              <span aria-hidden className="ml-0.5 inline-block animate-pulse text-ink-subtle">
                ▍
              </span>
            )}
          </>
        )}

        {message.status === "error" && message.error ? <TurnError error={message.error} /> : null}

        {message.toolCalls ? <ToolCalls toolCalls={message.toolCalls} /> : null}

        {message.citations ? <Citations citations={message.citations} /> : null}

        {message.meta ? <MetaChips meta={message.meta} /> : null}
      </div>
    </div>
  );
}
