import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import type { EvaluationRunStatus } from "@/graphql/generated";
import { formatTimestamp, formatUsd } from "@/lib/format";
import { formatPassRate, isRunActive, runStatusLabel, runStatusTone } from "@/lib/evaluations";

export type RunRow = {
  id: string;
  status: EvaluationRunStatus;
  caseCount: number;
  completedCount: number;
  provider: string;
  model: string;
  judgeModel: string | null;
  agentName: string | null;
  promptVersion: number | null;
  createdAt: string;
  startedAt: string | null;
  passRate: number | null;
  costUsd: string | null;
};

/** A dataset's runs, newest first. Each row links to its run page. */
export function RunsTable({ runs }: { runs: readonly RunRow[] }) {
  if (runs.length === 0) {
    return (
      <EmptyState
        icon="evaluation"
        title="No runs yet"
        description="Start a run above to see how the agent answers these cases."
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
          <tr>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Status
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Pass rate
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Prompt
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Model
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Cost
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Started
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {runs.map((run) => (
            <tr key={run.id} className="align-top">
              <td className="whitespace-nowrap px-5 py-3">
                <Link href={`/dashboard/evaluations/runs/${run.id}`} className="inline-flex hover:underline">
                  <Badge tone={runStatusTone(run.status)}>{runStatusLabel(run.status)}</Badge>
                  <span className="sr-only">, open run</span>
                </Link>
                {run.status !== "COMPLETED" ? (
                  <p className="mt-1 text-xs text-ink-subtle">
                    {run.completedCount} of {run.caseCount}
                  </p>
                ) : null}
              </td>
              <td className="px-5 py-3 text-ink">
                {/* A cancelled or failed run has a summary of the cases that ran; the
                    "n of m" under its status says how many that was. */}
                {!isRunActive(run.status) && run.passRate !== null ? (
                  formatPassRate(run.passRate)
                ) : (
                  <span className="text-ink-subtle">—</span>
                )}
              </td>
              <td className="px-5 py-3 text-ink">
                {run.promptVersion !== null ? `v${run.promptVersion}` : <span className="text-ink-subtle">Default</span>}
                {run.agentName ? <p className="text-xs text-ink-subtle">{run.agentName}</p> : null}
              </td>
              <td className="px-5 py-3">
                <span className="font-mono text-xs text-ink" title={`${run.provider} · ${run.model}`}>
                  {run.model}
                </span>
                {run.judgeModel ? (
                  <p className="font-mono text-xs text-ink-subtle">judge: {run.judgeModel}</p>
                ) : null}
              </td>
              <td className="whitespace-nowrap px-5 py-3 text-ink">{formatUsd(run.costUsd)}</td>
              <td className="whitespace-nowrap px-5 py-3 text-xs text-ink-subtle">
                {formatTimestamp(run.startedAt ?? run.createdAt)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
