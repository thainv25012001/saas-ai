"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { CreateAgentForm, type CreateAgentValues } from "@/components/agents/CreateAgentForm";
import {
  AgentsDocument,
  ConfiguredProvidersDocument,
  CreateAgentDocument,
  ProviderModelsDocument,
} from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { creatableProviders } from "@/lib/providers";
import { firstGraphQLError } from "@/lib/graphql-errors";

export default function AgentsPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching, error }, refetchAgents] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const [providersResult] = useQuery({
    query: ConfiguredProvidersDocument,
    pause: loading || !user,
  });
  const [createResult, createAgent] = useMutation(CreateAgentDocument);
  // The list is what you came for, so the create form is disclosed rather
  // than parked above it permanently.
  const [creating, setCreating] = useState(false);
  // Owned here rather than inside the form, because the model query keys on it.
  const [provider, setProvider] = useState("");
  // urql's mutation result has no reset: a failed create otherwise leaves
  // `createResult.error` set, so cancelling and reopening the form would
  // show a fresh, empty field already flagged with the stale message.
  const [errorDismissed, setErrorDismissed] = useState(false);

  // Memoised because the effect below depends on it: `?? []` is a new array
  // on every render, which would re-run the effect every render.
  const providers = useMemo(
    () => providersResult.data?.configuredProviders ?? [],
    [providersResult.data],
  );

  // Seeded from the API's answer rather than hardcoded: which providers are
  // offerable depends on which API keys the server holds, so there is nothing
  // sensible to guess before the query resolves.
  useEffect(() => {
    if (provider !== "") return;
    const first = creatableProviders(providers)[0];
    if (first) setProvider(first.id);
  }, [providers, provider]);

  const [modelsResult] = useQuery({
    query: ProviderModelsDocument,
    variables: { provider },
    pause: loading || !user || !provider,
  });

  const agents = data?.agents ?? [];
  const createError = errorDismissed ? null : firstGraphQLError(createResult.error);
  const queryError = firstGraphQLError(error);

  function openCreateForm() {
    setErrorDismissed(true);
    setCreating(true);
  }

  function cancelCreateForm() {
    setErrorDismissed(true);
    setCreating(false);
  }

  async function onCreate(values: CreateAgentValues) {
    setErrorDismissed(false);
    const result = await createAgent(values);
    if (!result.error) {
      setCreating(false);
      refetchAgents({ requestPolicy: "network-only" });
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Agents"
        description="Each agent is one assistant, with its own model, prompt and behaviour."
        actions={
          creating ? null : (
            <Button onClick={openCreateForm}>
              <Icon name="plus" size="md" />
              New agent
            </Button>
          )
        }
      />

      {creating ? (
        <Card>
          {/* The form holds the name and model in its own state. It unmounts
            * when `creating` goes false, so cancel-and-reopen gives a fresh
            * one with nothing left over. */}
          <CreateAgentForm
            providers={providers}
            provider={provider}
            onProviderChange={setProvider}
            models={modelsResult.data?.providerModels ?? []}
            modelsFetching={modelsResult.fetching}
            modelsFailed={modelsResult.error !== undefined}
            submitting={createResult.fetching}
            error={createError}
            onSubmit={onCreate}
            onCancel={cancelCreateForm}
          />
        </Card>
      ) : null}

      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}

      <Card>
        {fetching && !data ? (
          <LoadingState label="Loading agents…" />
        ) : queryError ? null : agents.length === 0 ? (
          <EmptyState
            icon="agent"
            title="No agents yet"
            description="Create one to configure a model, a prompt and a tone — then test it in the playground."
            action={<Button onClick={openCreateForm}>Create your first agent</Button>}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
                <tr>
                  <th scope="col" className="px-5 py-2.5 font-medium">
                    Name
                  </th>
                  <th scope="col" className="px-5 py-2.5 font-medium">
                    Slug
                  </th>
                  <th scope="col" className="px-5 py-2.5 font-medium">
                    Status
                  </th>
                  <th scope="col" className="px-5 py-2.5 font-medium">
                    Model
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {agents.map((agent) => (
                  <tr key={String(agent.id)}>
                    <td className="px-5 py-3">
                      <Link
                        href={`/dashboard/agents/${String(agent.id)}`}
                        className="font-medium text-ink hover:underline"
                      >
                        {agent.name}
                      </Link>
                    </td>
                    <td className="px-5 py-3 font-mono text-xs text-ink-muted">{agent.slug}</td>
                    <td className="px-5 py-3">
                      <Badge tone={agentStatusTone(agent.status)}>
                        {agentStatusLabel(agent.status)}
                      </Badge>
                    </td>
                    <td className="px-5 py-3 text-ink-muted">
                      {agent.provider} · {agent.model}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
