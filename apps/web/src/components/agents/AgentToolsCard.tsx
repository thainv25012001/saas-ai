import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";

export type AgentToolRow = {
  id: string;
  name: string;
  description: string | null;
  isEnabled: boolean;
};

/**
 * Task 8's whole reason for existing: `create_lead` is seeded and linked in
 * principle, but off by default for every agent, and until this shipped the
 * only way to turn it on was a raw database write (see
 * `apps/api/app/db/builtin_tools.py`'s module docstring). One row per
 * builtin this agent could call, each with a plain enable/disable button --
 * this app has no dedicated switch primitive, and a labelled `Button` that
 * already carries a `loading` state is `DocumentsTable`'s own Retry/Delete
 * pattern, not a new one invented here.
 *
 * Presentational, like `DocumentsTable`: the page owns the query and the
 * mutation, this owns rendering one screenful of rows and asking for a
 * tool's id back through `onToggle`.
 */
export function AgentToolsCard({
  tools,
  togglingId = null,
  onToggle,
}: {
  tools: readonly AgentToolRow[];
  /** The tool id currently mid-toggle, so only that row's button shows a
   * loading state instead of freezing the whole card. */
  togglingId?: string | null;
  onToggle: (toolId: string, nextEnabled: boolean) => void;
}) {
  return (
    <Card>
      <CardHeader
        title="Tools"
        description="What this agent may call mid-answer. Off until you turn it on here."
      />
      <CardBody className="space-y-0">
        {tools.length === 0 ? (
          <p className="text-sm text-ink-muted">No tools available yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {tools.map((tool) => (
              <li
                key={tool.id}
                className="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0"
              >
                <div className="min-w-0">
                  {/* The tool's own seeded description -- server-authored copy,
                    * not model or visitor input, but still a plain text child
                    * like everything else on this card. */}
                  <p className="font-mono text-sm text-ink">{tool.name}</p>
                  {tool.description ? (
                    <p className="mt-0.5 text-xs text-ink-muted">{tool.description}</p>
                  ) : null}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Badge tone={tool.isEnabled ? "success" : "neutral"}>
                    {tool.isEnabled ? "Enabled" : "Disabled"}
                  </Badge>
                  <Button
                    variant={tool.isEnabled ? "secondary" : "primary"}
                    size="sm"
                    onClick={() => onToggle(tool.id, !tool.isEnabled)}
                    loading={togglingId === tool.id}
                    loadingLabel="Saving…"
                  >
                    {tool.isEnabled ? "Disable" : "Enable"}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
