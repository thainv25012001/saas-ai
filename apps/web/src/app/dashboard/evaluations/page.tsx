"use client";

import { useRouter } from "next/navigation";
import { useMemo } from "react";
import { useMutation, useQuery } from "urql";
import { CreateDatasetForm, DatasetList } from "@/components/evaluations/DatasetList";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { CreateEvaluationDatasetDocument, EvaluationDatasetsDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";

export default function EvaluationsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  const [{ data, fetching, error }] = useQuery({
    query: EvaluationDatasetsDocument,
    pause: loading || !user,
    // A run finishing elsewhere changes each dataset's latest run; this list
    // is small and read on arrival, so it is always asked for fresh.
    requestPolicy: "cache-and-network",
  });
  const [createResult, createDataset] = useMutation(CreateEvaluationDatasetDocument);

  const datasets = useMemo(
    () =>
      (data?.evaluationDatasets ?? []).map((dataset) => ({
        id: dataset.id,
        name: dataset.name,
        description: dataset.description,
        caseCount: dataset.caseCount,
        latestRun: dataset.latestRun
          ? { status: dataset.latestRun.status, passRate: dataset.latestRun.summary?.passRate ?? null }
          : null,
      })),
    [data],
  );

  async function onCreate(values: { name: string; description: string | null }): Promise<boolean> {
    const result = await createDataset({ input: values });
    const id = result.data?.createEvaluationDataset.id;
    if (!id) return false;
    // Straight to the dataset: a dataset with no cases is only a name, and
    // adding cases is the next thing anyone does with it.
    router.push(`/dashboard/evaluations/${id}`);
    return true;
  }

  const queryError = firstGraphQLError(error);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Evaluations"
        description="Check how an agent answers a fixed set of questions, and whether a prompt or model change made it better or worse."
      />

      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}

      <Card>
        {fetching && !data ? (
          <LoadingState label="Loading datasets…" />
        ) : queryError ? null : (
          <DatasetList datasets={datasets} />
        )}
      </Card>

      <CreateDatasetForm
        onCreate={onCreate}
        submitting={createResult.fetching}
        error={firstGraphQLError(createResult.error)}
      />
    </div>
  );
}
