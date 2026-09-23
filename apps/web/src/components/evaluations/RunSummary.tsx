import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import type { EvaluationRunStatus } from "@/graphql/generated";
import { formatLatency, formatUsd } from "@/lib/format";
import { formatPassRate, isRunActive, scorerLabel } from "@/lib/evaluations";

export type RunSummaryData = {
  passed: number;
  failed: number;
  errored: number;
  passRate: number | null;
  costUsd: string | null;
  meanLatencyMs: number | null;
  scorers: readonly { name: string; mean: number | null; passed: number; applicable: number }[];
};

export type RunSummaryRun = {
  status: EvaluationRunStatus;
  caseCount: number;
  completedCount: number;
  /** A failed run's reason (outside any one case). Server-written, but it can
   * quote a provider's error text -- JSX text only. */
  error: string | null;
  /** Written when the run completes; `null` before that. */
  summary: RunSummaryData | null;
};

function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-card border border-line bg-surface px-4 py-3">
      <p className="text-xs text-ink-subtle">{label}</p>
      <p className="mt-1 text-lg font-semibold text-ink">{value}</p>
      {detail ? <p className="mt-0.5 text-xs text-ink-muted">{detail}</p> : null}
    </div>
  );
}

/**
 * The run page's headline: progress while it runs, the summary tiles once it
 * has one. Every tile shows `—` rather than a zero it does not know -- a
 * `null` cost means "a model was unpriced", not "free".
 */
export function RunSummary({
  run,
  onCancel,
  cancelling = false,
}: {
  run: RunSummaryRun;
  onCancel?: () => void;
  cancelling?: boolean;
}) {
  const active = isRunActive(run.status);
  const summary = run.summary;
  const percent = run.caseCount > 0 ? Math.round((run.completedCount / run.caseCount) * 100) : 0;

  return (
    <div className="space-y-4">
      {active ? (
        <div className="space-y-2 rounded-card border border-line bg-surface px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-ink">
              {run.status === "PENDING" ? "Queued — waiting for a worker." : "Running"}{" "}
              <span className="text-ink-muted">
                {run.completedCount} of {run.caseCount} cases
              </span>
            </p>
            {onCancel ? (
              <Button variant="danger" size="sm" onClick={onCancel} loading={cancelling} loadingLabel="Cancelling…">
                Cancel run
              </Button>
            ) : null}
          </div>
          <div
            role="progressbar"
            aria-label="Cases completed"
            aria-valuemin={0}
            aria-valuemax={run.caseCount}
            aria-valuenow={run.completedCount}
            className="h-2 overflow-hidden rounded-control bg-surface-muted"
          >
            <div className="h-full bg-info transition-[width]" style={{ width: `${percent}%` }} />
          </div>
          <p className="text-xs text-ink-subtle">
            Cases run one at a time. Cancelling stops before the next case; the one in flight finishes.
          </p>
        </div>
      ) : null}

      {run.status === "FAILED" && run.error ? (
        <Alert tone="danger" title="The run failed">
          {run.error}
        </Alert>
      ) : null}
      {run.status === "CANCELLED" ? (
        <Alert tone="info">
          Cancelled after {run.completedCount} of {run.caseCount} cases. The results below are the ones that ran.
        </Alert>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label="Pass rate" value={summary ? formatPassRate(summary.passRate) : "—"} />
        <Tile
          label="Passed / failed"
          value={summary ? `${summary.passed} / ${summary.failed}` : "—"}
          detail={summary ? `${summary.errored} errored (counted as failed)` : undefined}
        />
        <Tile
          label="Cost"
          value={summary ? formatUsd(summary.costUsd) : "—"}
          detail={summary && summary.costUsd === null ? "A model in this run has no price." : undefined}
        />
        <Tile label="Mean latency" value={summary ? formatLatency(summary.meanLatencyMs) : "—"} />
      </div>

      {summary && summary.scorers.length > 0 ? (
        <div className="overflow-x-auto rounded-card border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">
                  Scorer
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Mean score
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Passed
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {summary.scorers.map((scorer) => (
                <tr key={scorer.name}>
                  <td className="px-4 py-2 text-ink">{scorerLabel(scorer.name)}</td>
                  <td className="px-4 py-2 text-ink">{formatPassRate(scorer.mean)}</td>
                  <td className="px-4 py-2 text-ink-muted">
                    {scorer.passed} of {scorer.applicable}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
