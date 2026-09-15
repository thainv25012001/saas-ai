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

export type ChatMessageData = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status: "streaming" | "done" | "error";
  error?: ChatMessageError;
  meta?: ChatMessageMeta;
};

/** `cost_usd` is `null` whenever the model isn't in the pricing table (see
 * `docs/PHASE-2.md` §5) -- that's a real, honest state, not zero cost. */
function formatCost(costUsd: string | null): string {
  if (costUsd === null) return "not priced";
  const value = Number(costUsd);
  if (Number.isNaN(value)) return "not priced";
  return `$${value.toFixed(4)}`;
}

export function ChatMessage({ message }: { message: ChatMessageData }) {
  const isUser = message.role === "user";
  // An assistant turn stopped (via the Stop button, or a dropped connection)
  // before any token arrived has nothing to render in the text paragraph
  // below -- without this, that shows up as a near-invisible empty bubble.
  const stoppedWithNoText = !isUser && message.status === "done" && message.text === "" && !message.meta;

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-2xl rounded-xl px-4 py-3 text-sm ${
          isUser
            ? "bg-slate-900 text-white"
            : "border border-slate-200 bg-white text-slate-900"
        }`}
      >
        {stoppedWithNoText ? (
          <p className="italic text-slate-400">Stopped before any response arrived.</p>
        ) : (
          <p className="whitespace-pre-wrap break-words">
            {message.text}
            {message.status === "streaming" && (
              <span aria-hidden className="ml-0.5 inline-block animate-pulse text-slate-400">
                ▍
              </span>
            )}
          </p>
        )}

        {message.status === "error" && message.error && (
          <p role="alert" className="mt-2 rounded-md bg-red-50 p-2 text-xs text-red-700">
            {message.error.message}
          </p>
        )}

        {message.meta && (
          <p className="mt-2 border-t border-slate-100 pt-2 text-xs text-slate-500">
            {message.meta.model} · {message.meta.usage.input_tokens} in /{" "}
            {message.meta.usage.output_tokens} out tokens · {formatCost(message.meta.costUsd)} ·{" "}
            {message.meta.latencyMs}ms
          </p>
        )}
      </div>
    </div>
  );
}
