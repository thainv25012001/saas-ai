"use client";

import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { CaseForm, type CaseValues } from "@/components/evaluations/CaseForm";
import { type CaseRow, CasesTable } from "@/components/evaluations/CasesTable";
import { RunsTable } from "@/components/evaluations/RunsTable";
import { type ModelsState, StartRunForm, type VersionsState } from "@/components/evaluations/StartRunForm";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import {
  AgentPromptDocument,
  AgentsDocument,
  ConfiguredProvidersDocument,
  CreateEvaluationCaseDocument,
  DeleteEvaluationCaseDocument,
  DeleteEvaluationDatasetDocument,
  DocumentsDocument,
  EvaluationCasesDocument,
  EvaluationDatasetDocument,
  EvaluationRunsDocument,
  ProductPickerDocument,
  PromptVersionsDocument,
  ProviderModelsDocument,
  UpdateEvaluationCaseDocument,
  UpdateEvaluationDatasetDocument,
} from "@/graphql/generated";
import { API_URL, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { isRunActive, type StartRunPayload, startEvaluationRun } from "@/lib/evaluations";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { usePollWhile } from "@/lib/use-document-polling";
import { useTabHidden } from "@/lib/use-tab-hidden";

const RECENT_RUNS = 20;
const PRODUCT_SEARCH_DEBOUNCE_MS = 300;
/** The API's list cap -- the most documents or products one page returns. */
const LIST_LIMIT = 100;

/** `StartRunForm`'s model list for one provider. Module-level so its identity
 * is stable across renders. */
function useProviderModels(provider: string): ModelsState {
  const { user, loading } = useAuth();
  const [{ data, fetching, error }] = useQuery({
    query: ProviderModelsDocument,
    variables: { provider },
    pause: loading || !user || !provider,
  });
  return { options: data?.providerModels ?? [], fetching, failed: error !== undefined };
}

/** An agent's prompt versions, newest first: the agent names its prompt, the
 * prompt lists its versions. */
function useAgentVersions(agentId: string): VersionsState {
  const { user, loading } = useAuth();
  const [agentResult] = useQuery({
    query: AgentPromptDocument,
    variables: { id: agentId },
    pause: loading || !user || !agentId,
  });
  const promptId = agentResult.data?.agent.promptId ?? null;
  const [versionsResult] = useQuery({
    query: PromptVersionsDocument,
    variables: { id: promptId ?? "" },
    pause: loading || !user || !promptId,
    // A version drafted or activated on another tab must show up here.
    requestPolicy: "cache-and-network",
  });
  return {
    versions: promptId ? (versionsResult.data?.prompt.versions ?? []) : [],
    fetching: agentResult.fetching || versionsResult.fetching,
    failed: agentResult.error !== undefined || versionsResult.error !== undefined,
    noPrompt: agentResult.data !== undefined && promptId === null,
  };
}

export default function EvaluationDatasetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { user, loading, accessToken, setAccessToken } = useAuth();
  const router = useRouter();
  const paused = loading || !user;

  const [datasetResult] = useQuery({ query: EvaluationDatasetDocument, variables: { id }, pause: paused });
  const [casesResult, refetchCases] = useQuery({
    query: EvaluationCasesDocument,
    variables: { datasetId: id },
    pause: paused,
  });
  const [runsResult, refetchRuns] = useQuery({
    query: EvaluationRunsDocument,
    variables: { datasetId: id, limit: RECENT_RUNS },
    pause: paused,
    requestPolicy: "cache-and-network",
  });
  const [agentsResult] = useQuery({ query: AgentsDocument, pause: paused });
  const [providersResult] = useQuery({ query: ConfiguredProvidersDocument, pause: paused });
  const [documentsResult] = useQuery({
    query: DocumentsDocument,
    variables: { status: "READY", limit: LIST_LIMIT },
    pause: paused,
  });

  // Product search for the case form, debounced like the Products page.
  const [productSearchInput, setProductSearchInput] = useState("");
  const [productSearch, setProductSearch] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setProductSearch(productSearchInput), PRODUCT_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [productSearchInput]);
  const [productSearchResult] = useQuery({
    query: ProductPickerDocument,
    variables: { search: productSearch, limit: 10 },
    pause: paused || productSearch === "",
  });
  // Names for the product ids cases already expect. There is no lookup by id,
  // so the first page of the catalogue stands in; anything beyond it shows a
  // short id instead of a name.
  const [productLabelsResult] = useQuery({
    query: ProductPickerDocument,
    variables: { search: null, limit: LIST_LIMIT },
    pause: paused,
  });

  const [updateDatasetResult, updateDataset] = useMutation(UpdateEvaluationDatasetDocument);
  const [deleteDatasetResult, deleteDataset] = useMutation(DeleteEvaluationDatasetDocument);
  const [createCaseResult, createCase] = useMutation(CreateEvaluationCaseDocument);
  const [updateCaseResult, updateCase] = useMutation(UpdateEvaluationCaseDocument);
  const [, deleteCase] = useMutation(DeleteEvaluationCaseDocument);

  const dataset = datasetResult.data?.evaluationDataset;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [datasetSaved, setDatasetSaved] = useState(false);
  useEffect(() => {
    if (!dataset) return;
    setName(dataset.name);
    setDescription(dataset.description ?? "");
  }, [dataset]);

  const [editing, setEditing] = useState<CaseRow | null>(null);
  const [deletingCaseId, setDeletingCaseId] = useState<string | null>(null);
  const [caseActionError, setCaseActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const cases: CaseRow[] = useMemo(
    () =>
      (casesResult.data?.evaluationCases ?? []).map((row) => ({
        id: row.id,
        question: row.question,
        referenceAnswer: row.referenceAnswer,
        requiredPhrases: row.requiredPhrases,
        expectedToolNames: row.expectedToolNames,
        expectedDocumentIds: row.expectedDocumentIds,
        expectedProductIds: row.expectedProductIds,
        tags: row.tags,
      })),
    [casesResult.data],
  );
  const runs = useMemo(() => runsResult.data?.evaluationRuns ?? [], [runsResult.data]);
  const activeRun = runs.find((run) => isRunActive(run.status)) ?? null;

  // Keep the runs list honest while one is in flight -- same interval, same
  // stop-when-hidden rule as the run page itself.
  const tabHidden = useTabHidden();
  const pollRuns = useCallback(() => refetchRuns({ requestPolicy: "network-only" }), [refetchRuns]);
  usePollWhile(activeRun !== null && !tabHidden, pollRuns);

  const documents = useMemo(
    () => (documentsResult.data?.documents ?? []).map((doc) => ({ id: doc.id, title: doc.title })),
    [documentsResult.data],
  );
  const productLabels = useMemo(
    () =>
      Object.fromEntries((productLabelsResult.data?.products ?? []).map((product) => [product.id, product.name])),
    [productLabelsResult.data],
  );
  const agents = useMemo(
    () =>
      (agentsResult.data?.agents ?? []).map((agent) => ({
        id: agent.id,
        name: agent.name,
        provider: agent.provider,
        model: agent.model,
      })),
    [agentsResult.data],
  );

  async function onSaveDataset(event: React.FormEvent) {
    event.preventDefault();
    setDatasetSaved(false);
    // An empty string, not null, clears the description: the API reads a
    // null description as "leave unchanged".
    const result = await updateDataset({ id, input: { name: name.trim(), description: description.trim() } });
    if (!result.error) setDatasetSaved(true);
  }

  async function onDeleteDataset() {
    if (!dataset) return;
    if (!window.confirm(`Delete dataset "${dataset.name}", its cases and every run of it? This cannot be undone.`)) {
      return;
    }
    const result = await deleteDataset({ id });
    if (!result.error) router.push("/dashboard/evaluations");
  }

  function caseInput(values: CaseValues) {
    return {
      question: values.question,
      referenceAnswer: values.referenceAnswer,
      requiredPhrases: values.requiredPhrases,
      expectedToolNames: values.expectedToolNames,
      expectedDocumentIds: values.expectedDocumentIds,
      expectedProductIds: values.expectedProductIds,
      tags: values.tags,
    };
  }

  async function onCreateCase(values: CaseValues): Promise<boolean> {
    const result = await createCase({ datasetId: id, input: caseInput(values) });
    if (result.error) return false;
    refetchCases({ requestPolicy: "network-only" });
    return true;
  }

  async function onUpdateCase(values: CaseValues): Promise<boolean> {
    if (!editing) return false;
    const result = await updateCase({ id: editing.id, input: caseInput(values) });
    if (result.error) return false;
    setEditing(null);
    refetchCases({ requestPolicy: "network-only" });
    return true;
  }

  async function onDeleteCase(caseId: string) {
    if (!window.confirm("Delete this case? Past runs keep their results for it.")) return;
    setCaseActionError(null);
    setDeletingCaseId(caseId);
    const result = await deleteCase({ id: caseId });
    setDeletingCaseId(null);
    if (result.error) {
      setCaseActionError(firstGraphQLError(result.error));
      return;
    }
    if (editing?.id === caseId) setEditing(null);
    refetchCases({ requestPolicy: "network-only" });
  }

  async function onStart(payload: StartRunPayload) {
    if (!accessToken) return;
    setStartError(null);
    setStarting(true);
    try {
      const run = await startEvaluationRun(payload, {
        accessToken,
        apiUrl: API_URL,
        onAccessToken: setAccessToken,
      });
      router.push(`/dashboard/evaluations/runs/${run.id}`);
    } catch (err) {
      setStartError((err as ApiError).message);
      refetchRuns({ requestPolicy: "network-only" });
    } finally {
      setStarting(false);
    }
  }

  const datasetError = firstGraphQLError(datasetResult.error);
  if (datasetResult.fetching && !dataset) return <LoadingState label="Loading dataset…" />;
  if (datasetError || !dataset) {
    return <Alert tone="danger">{datasetError ?? "Dataset not found."}</Alert>;
  }

  const casesError = firstGraphQLError(casesResult.error);
  const runsError = firstGraphQLError(runsResult.error);

  return (
    <div className="space-y-6">
      <PageHeader
        title={dataset.name}
        description={dataset.description ?? undefined}
        breadcrumb={[{ href: "/dashboard/evaluations", label: "Evaluations" }]}
      />

      <Card>
        <CardHeader
          title="Cases"
          description={`${cases.length} ${cases.length === 1 ? "case" : "cases"}. A run asks each one, in turn, of the agent you choose.`}
        />
        {caseActionError ? (
          <div className="px-5 pt-4">
            <Alert tone="danger">{caseActionError}</Alert>
          </div>
        ) : null}
        {casesError ? (
          <div className="p-5">
            <Alert tone="danger">{casesError}</Alert>
          </div>
        ) : casesResult.fetching && !casesResult.data ? (
          <LoadingState label="Loading cases…" />
        ) : (
          <CasesTable
            cases={cases}
            onEdit={setEditing}
            onDelete={onDeleteCase}
            deletingId={deletingCaseId}
            editingId={editing?.id ?? null}
          />
        )}
      </Card>

      <Card>
        <CardHeader
          title={editing ? "Edit case" : "Add a case"}
          description={
            editing
              ? "Saving replaces the case. Past runs keep the results they recorded."
              : "A question, and what a good answer has to contain."
          }
        />
        <CardBody>
          <CaseForm
            // Remount per case, so the form's own state starts from that case.
            key={editing?.id ?? "new"}
            initial={editing ?? undefined}
            documents={documents}
            productOptions={productSearchResult.data?.products ?? []}
            productLabels={productLabels}
            onProductSearch={setProductSearchInput}
            onSubmit={editing ? onUpdateCase : onCreateCase}
            onCancel={editing ? () => setEditing(null) : undefined}
            submitting={editing ? updateCaseResult.fetching : createCaseResult.fetching}
            error={firstGraphQLError(editing ? updateCaseResult.error : createCaseResult.error)}
            submitLabel={editing ? "Save case" : "Add case"}
          />
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Start a run"
          description="Runs every case against an agent, as if a customer asked it. Nothing it does is kept: no conversation, no lead."
        />
        <CardBody>
          {agentsResult.fetching && !agentsResult.data ? (
            <LoadingState label="Loading agents…" />
          ) : (
            <StartRunForm
              datasetId={id}
              caseCount={cases.length}
              agents={agents}
              providers={providersResult.data?.configuredProviders ?? []}
              useModels={useProviderModels}
              useVersions={useAgentVersions}
              onStart={onStart}
              starting={starting}
              error={startError}
              blockedReason={
                activeRun ? "A run of this dataset is in progress. Start another once it finishes or is cancelled." : null
              }
            />
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Runs" description="Newest first. Open one to see each case's result, or compare two." />
        {runsError ? (
          <div className="p-5">
            <Alert tone="danger">{runsError}</Alert>
          </div>
        ) : runsResult.fetching && !runsResult.data ? (
          <LoadingState label="Loading runs…" />
        ) : (
          <RunsTable
            runs={runs.map((run) => ({
              id: run.id,
              status: run.status,
              caseCount: run.caseCount,
              completedCount: run.completedCount,
              provider: run.provider,
              model: run.model,
              judgeModel: run.judgeModel,
              agentName: run.agentName,
              promptVersion: run.promptVersion,
              createdAt: run.createdAt,
              startedAt: run.startedAt,
              passRate: run.summary?.passRate ?? null,
              costUsd: run.summary?.costUsd ?? null,
            }))}
          />
        )}
      </Card>

      <Card>
        <form onSubmit={onSaveDataset}>
          <CardHeader title="Dataset" description="Its name and what it is for." />
          <CardBody className="space-y-4">
            {firstGraphQLError(updateDatasetResult.error) ? (
              <Alert tone="danger">{firstGraphQLError(updateDatasetResult.error)}</Alert>
            ) : null}
            <Field label="Name" required>
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  maxLength={200}
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    setDatasetSaved(false);
                  }}
                />
              )}
            </Field>
            <Field label="Description">
              {(control) => (
                <Textarea
                  {...control}
                  rows={2}
                  maxLength={2000}
                  value={description}
                  onChange={(event) => {
                    setDescription(event.target.value);
                    setDatasetSaved(false);
                  }}
                />
              )}
            </Field>
          </CardBody>
          <CardFooter>
            <Button type="submit" disabled={!name.trim()} loading={updateDatasetResult.fetching} loadingLabel="Saving…">
              Save
            </Button>
            {datasetSaved ? <span className="text-sm text-success">Saved.</span> : null}
          </CardFooter>
        </form>
      </Card>

      <Card tone="danger">
        <CardHeader
          title="Delete dataset"
          description="Removes the dataset, its cases and every run of it."
          actions={
            <Button variant="danger" onClick={onDeleteDataset} loading={deleteDatasetResult.fetching} loadingLabel="Deleting…">
              Delete
            </Button>
          }
        />
        {firstGraphQLError(deleteDatasetResult.error) ? (
          <CardBody>
            <Alert tone="danger">{firstGraphQLError(deleteDatasetResult.error)}</Alert>
          </CardBody>
        ) : null}
      </Card>
    </div>
  );
}
