"use client";

import { use, useCallback, useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { ResultsTable } from "@/components/evaluations/ResultsTable";
import { RunSummary } from "@/components/evaluations/RunSummary";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card, CardHeader } from "@/components/ui/Card";
import { Select } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import {
  CancelEvaluationRunDocument,
  DocumentsDocument,
  EvaluationDatasetDocument,
  EvaluationRunDocument,
  EvaluationRunOutcomesDocument,
  EvaluationRunsDocument,
} from "@/graphql/generated";
import { useAuth } from "@/lib/auth";
import {
  compareRuns,
  type ComparisonState,
  formatPassRate,
  runStatusLabel,
  runStatusTone,
  shouldPollRun,
} from "@/lib/evaluations";
import { formatTimestamp } from "@/lib/format";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { providerLabel } from "@/lib/providers";
import { usePollWhile } from "@/lib/use-document-polling";
import { useTabHidden } from "@/lib/use-tab-hidden";

/** How many earlier runs the Compare select offers. */
const COMPARABLE_RUNS = 50;

export default function EvaluationRunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = use(params);
  const { user, loading } = useAuth();
  const paused = loading || !user;

  const [{ data, fetching, error }, refetchRun] = useQuery({
    query: EvaluationRunDocument,
    variables: { id: runId },
    pause: paused,
  });
  const run = data?.evaluationRun ?? null;

  // Poll while the run can still move, stop when it settles or the tab is
  // hidden -- the Products imports pattern (docs/DESIGN.md, Polling).
  const tabHidden = useTabHidden();
  const poll = useCallback(() => refetchRun({ requestPolicy: "network-only" }), [refetchRun]);
  usePollWhile(shouldPollRun(run?.status, tabHidden), poll);

  const datasetId = run?.datasetId ?? "";
  const [datasetResult] = useQuery({
    query: EvaluationDatasetDocument,
    variables: { id: datasetId },
    pause: paused || !datasetId,
  });
  const [otherRunsResult] = useQuery({
    query: EvaluationRunsDocument,
    variables: { datasetId, limit: COMPARABLE_RUNS },
    pause: paused || !datasetId,
  });
  const [documentsResult] = useQuery({ query: DocumentsDocument, variables: { limit: 100 }, pause: paused });

  const [compareWith, setCompareWith] = useState("");
  const [baseResult] = useQuery({
    query: EvaluationRunOutcomesDocument,
    variables: { id: compareWith },
    pause: paused || !compareWith,
  });

  const [cancelResult, cancelRun] = useMutation(CancelEvaluationRunDocument);

  const comparableRuns = useMemo(
    () =>
      (otherRunsResult.data?.evaluationRuns ?? []).filter(
        (other) => other.id !== runId && other.status === "COMPLETED",
      ),
    [otherRunsResult.data, runId],
  );

  const results = useMemo(() => run?.results ?? [], [run]);
  const comparison = useMemo(() => {
    const base = baseResult.data?.evaluationRun?.results;
    if (!compareWith || !base) return null;
    const states: Record<string, ComparisonState> = {};
    for (const entry of compareRuns(base, results)) states[entry.candidate.id] = entry.state;
    return states;
  }, [baseResult.data, compareWith, results]);

  const documentTitles = useMemo(
    () => Object.fromEntries((documentsResult.data?.documents ?? []).map((doc) => [doc.id, doc.title])),
    [documentsResult.data],
  );

  async function onCancel() {
    if (!window.confirm("Cancel this run? Cases already scored keep their results.")) return;
    await cancelRun({ id: runId });
    refetchRun({ requestPolicy: "network-only" });
  }

  const queryError = firstGraphQLError(error);
  if (fetching && !data) return <LoadingState label="Loading run…" />;
  if (queryError) return <Alert tone="danger">{queryError}</Alert>;
  if (!run) return <Alert tone="danger">Run not found.</Alert>;

  const datasetName = datasetResult.data?.evaluationDataset.name;

  return (
    <div className="space-y-6">
      <PageHeader
        title={`Run · ${formatTimestamp(run.startedAt ?? run.createdAt)}`}
        breadcrumb={[
          { href: "/dashboard/evaluations", label: "Evaluations" },
          { href: `/dashboard/evaluations/${run.datasetId}`, label: datasetName ?? "Dataset" },
        ]}
        meta={
          <>
            <Badge tone={runStatusTone(run.status)}>{runStatusLabel(run.status)}</Badge>
            {run.agentName ? <Badge>{run.agentName}</Badge> : null}
            <Badge>{run.promptVersion !== null ? `Prompt v${run.promptVersion}` : "Default prompt"}</Badge>
            <Badge title={`${providerLabel(run.provider)} · ${run.model}`}>
              <span className="font-mono">{run.model}</span>
            </Badge>
            {run.judgeModel ? (
              <Badge title={`Judge: ${providerLabel(run.judgeProvider ?? "")} · ${run.judgeModel}`}>
                judge: <span className="font-mono">{run.judgeModel}</span>
              </Badge>
            ) : null}
          </>
        }
      />

      {firstGraphQLError(cancelResult.error) ? (
        <Alert tone="danger">{firstGraphQLError(cancelResult.error)}</Alert>
      ) : null}

      <RunSummary
        run={{
          status: run.status,
          caseCount: run.caseCount,
          completedCount: run.completedCount,
          error: run.error,
          summary: run.summary,
        }}
        onCancel={onCancel}
        cancelling={cancelResult.fetching}
      />

      <Card>
        <CardHeader
          title="Results"
          description="One row per case. Open a row to see the answer, why each scorer decided what it did, and the tools it called."
          actions={
            <Select
              aria-label="Compare with"
              width="auto"
              className="min-w-56"
              value={compareWith}
              onChange={(event) => setCompareWith(event.target.value)}
              disabled={comparableRuns.length === 0}
              title={comparableRuns.length === 0 ? "No other completed run of this dataset yet" : undefined}
            >
              <option value="">
                {comparableRuns.length === 0 ? "No run to compare with" : "Don’t compare"}
              </option>
              {comparableRuns.map((other) => (
                <option key={other.id} value={other.id}>
                  {`${formatTimestamp(other.startedAt ?? other.createdAt)} · ${
                    other.promptVersion !== null ? `v${other.promptVersion}` : "default"
                  } · ${other.model} · ${formatPassRate(other.summary?.passRate)}`}
                </option>
              ))}
            </Select>
          }
        />
        {compareWith && baseResult.fetching && !comparison ? <LoadingState label="Loading the other run…" /> : null}
        {firstGraphQLError(baseResult.error) ? (
          <div className="px-5 pt-4">
            <Alert tone="danger">{firstGraphQLError(baseResult.error)}</Alert>
          </div>
        ) : null}
        <ResultsTable results={results} comparison={comparison} documentTitles={documentTitles} />
      </Card>
    </div>
  );
}
