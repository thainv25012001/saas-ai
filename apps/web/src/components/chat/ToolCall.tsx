import { Badge } from "@/components/ui/Badge";
import { cn } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";

export type ToolCallData = {
  id: string;
  name: string;
  /** Untrusted -- see the module docstring on `ToolCall` in `@/lib/sse`.
   * Rendered as JSX text only, never through `dangerouslySetInnerHTML` or a
   * markdown pass. */
  arguments: Record<string, unknown>;
  /** `running` from `tool_call_start` until the matching `tool_call_end`
   * arrives; a turn that stops mid-call (an aborted stream) never resolves
   * this, which is deliberate -- see `ChatMessage`'s handling. */
  status: "running" | "done";
  /** Untrusted excerpt of the tool's own result -- absent until `status`
   * is `"done"`. */
  result?: string;
  isError?: boolean;
};

/** One line describing what the model asked for, without a JSON dump. Each
 * argument value is stringified minimally -- a plain string prints as-is, an
 * object/array falls back to `JSON.stringify` so nothing is silently
 * dropped, and there is always something to show even for an empty call. */
function formatArgumentValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function summarizeArguments(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "no arguments";
  return entries.map(([key, value]) => `${key}: ${formatArgumentValue(value)}`).join(" · ");
}

/**
 * One tool call, rendered inline in the transcript: name, a readable
 * one-line argument summary, and the result collapsed behind a native
 * `<details>` (no extra state, and it works without JS-driven a11y wiring).
 *
 * `arguments` and `result` both quote model output that can itself quote an
 * uploaded document's content or a visitor's own typed text (`create_lead`).
 * Every value below reaches the page only as a JSX text child -- there is no
 * `dangerouslySetInnerHTML` and no markdown pass anywhere in this file, so a
 * `<script>` in either one prints as literal characters instead of running.
 */
export function ToolCall({ data }: { data: ToolCallData }) {
  const failed = data.status === "done" && data.isError === true;
  return (
    <div
      className={cn(
        "rounded-control border px-3 py-2 text-xs",
        failed ? "border-danger-line bg-danger-surface" : "border-line bg-surface-muted",
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Icon name="tool" size="sm" className="text-ink-subtle" />
        <span className="font-mono font-medium text-ink">{data.name}</span>
        {data.status === "running" ? (
          <Badge tone="info">Running…</Badge>
        ) : failed ? (
          <Badge tone="danger">Failed</Badge>
        ) : (
          <Badge tone="success">Done</Badge>
        )}
      </div>
      <p className="mt-1 text-ink-muted">{summarizeArguments(data.arguments)}</p>
      {data.status === "done" && data.result !== undefined ? (
        <details className="mt-1.5">
          <summary className="cursor-pointer select-none text-ink-subtle hover:text-ink">
            Result
          </summary>
          <p className="mt-1 whitespace-pre-wrap break-words text-ink-muted">{data.result}</p>
        </details>
      ) : null}
    </div>
  );
}
