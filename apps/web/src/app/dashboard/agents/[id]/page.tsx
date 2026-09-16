"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "urql";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { ModelPicker } from "@/components/agents/ModelPicker";
import { Input, Select } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import {
  AgentDocument,
  AgentStatus,
  DeleteAgentDocument,
  ProviderModelsDocument,
  UpdateAgentConfigDocument,
  UpdateAgentDocument,
} from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { PROVIDERS, providerLabel } from "@/lib/providers";
import { firstGraphQLError } from "@/lib/graphql-errors";

const STATUSES: AgentStatus[] = ["DRAFT", "ACTIVE", "DISABLED"];

export default function AgentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { user, loading } = useAuth();
  const router = useRouter();

  const [{ data, fetching, error }, refetchAgent] = useQuery({
    query: AgentDocument,
    variables: { id },
    pause: loading || !user,
  });

  const [updateAgentResult, updateAgent] = useMutation(UpdateAgentDocument);
  const [updateConfigResult, updateAgentConfig] = useMutation(UpdateAgentConfigDocument);
  const [deleteResult, deleteAgent] = useMutation(DeleteAgentDocument);

  const agent = data?.agent;

  const [name, setName] = useState("");
  const [status, setStatus] = useState<AgentStatus>("DRAFT");
  // Explicitly `string`: `PROVIDERS` is a const tuple, so inference would
  // narrow this to the literal "fake" and reject the agent's own provider.
  const [provider, setProvider] = useState<string>(PROVIDERS[0]);
  const [model, setModel] = useState("");

  // Re-runs whenever the provider dropdown changes, so the model list always
  // belongs to the provider actually selected rather than the one the agent was
  // loaded with.
  const [modelsResult] = useQuery({
    query: ProviderModelsDocument,
    variables: { provider },
    pause: loading || !user || !provider,
  });

  const [temperature, setTemperature] = useState(0.3);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [agentSaved, setAgentSaved] = useState(false);

  const [tone, setTone] = useState("");
  const [retrievalTopK, setRetrievalTopK] = useState(5);
  const [maxAgentSteps, setMaxAgentSteps] = useState(6);
  const [configSaved, setConfigSaved] = useState(false);

  useEffect(() => {
    if (!agent) return;
    setName(agent.name);
    setStatus(agent.status);
    setProvider(agent.provider);
    setModel(agent.model);
    setTemperature(agent.temperature);
    setMaxTokens(agent.maxTokens);
    if (agent.config) {
      setTone(agent.config.tone);
      setRetrievalTopK(agent.config.retrievalTopK);
      setMaxAgentSteps(agent.config.maxAgentSteps);
    }
  }, [agent]);

  async function onSubmitAgent(event: React.FormEvent) {
    event.preventDefault();
    setAgentSaved(false);
    const result = await updateAgent({
      id,
      input: { name, status, provider, model, temperature, maxTokens },
    });
    if (!result.error) {
      setAgentSaved(true);
      refetchAgent({ requestPolicy: "network-only" });
      setTimeout(() => setAgentSaved(false), 2000);
    }
  }

  async function onSubmitConfig(event: React.FormEvent) {
    event.preventDefault();
    setConfigSaved(false);
    const result = await updateAgentConfig({
      agentId: id,
      input: { tone, retrievalTopK, maxAgentSteps },
    });
    if (!result.error) {
      setConfigSaved(true);
      refetchAgent({ requestPolicy: "network-only" });
      setTimeout(() => setConfigSaved(false), 2000);
    }
  }

  async function onDelete() {
    if (!agent) return;
    if (!window.confirm(`Delete agent "${agent.name}"? This cannot be undone.`)) return;
    const result = await deleteAgent({ id });
    if (!result.error) {
      router.push("/dashboard/agents");
    }
  }

  const agentError = firstGraphQLError(updateAgentResult.error);
  const configError = firstGraphQLError(updateConfigResult.error);
  const deleteError = firstGraphQLError(deleteResult.error);

  if (fetching && !agent) return <LoadingState label="Loading agent…" />;

  if (error && !agent) {
    return (
      <Alert tone="danger" title="Could not load this agent">
        {firstGraphQLError(error) ?? "Please reload the page."}
      </Alert>
    );
  }

  if (!agent) return null;

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        breadcrumb={[{ href: "/dashboard/agents", label: "Agents" }]}
        title={agent.name}
        meta={
          <>
            <Badge tone={agentStatusTone(agent.status)}>{agentStatusLabel(agent.status)}</Badge>
            <span className="font-mono text-xs text-ink-subtle">{agent.slug}</span>
          </>
        }
        actions={
          <ButtonLink href={`/dashboard/playground?agentId=${String(agent.id)}`}>
            Test in playground
          </ButtonLink>
        }
      />

      <form onSubmit={onSubmitAgent} className="space-y-6">
        <Card>
          <CardHeader
            title="Identity"
            description="What this agent is called, and whether it is live. Saved together with Model, below."
          />
          <CardBody className="space-y-4">
            {agentError ? <Alert tone="danger">{agentError}</Alert> : null}

            <Field label="Name" required>
              {(control) => (
                <Input {...control} type="text" value={name} onChange={(e) => setName(e.target.value)} />
              )}
            </Field>

            <Field
              label="Status"
              description="Draft is configurable but not live. Disabled stops it answering."
            >
              {(control) => (
                <Select
                  {...control}
                  value={status}
                  onChange={(e) => setStatus(e.target.value as AgentStatus)}
                >
                  {STATUSES.map((option) => (
                    <option key={option} value={option}>
                      {agentStatusLabel(option)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Model" description="Which model answers, and how freely." />
          <CardBody className="space-y-4">
            <Field
              label="Provider"
              description="Fake answers offline with a canned reply and costs nothing — useful for wiring, useless for real answers. Switch to OpenAI, Anthropic or OpenRouter once the matching API key is set. OpenRouter models ending in `:free` cost nothing but are rate limited."
            >
              {(control) => (
                <Select {...control} value={provider} onChange={(e) => setProvider(e.target.value)}>
                  {PROVIDERS.map((option) => (
                    <option key={option} value={option}>
                      {providerLabel(option)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field
              label="Model"
              description={
                provider === "openrouter"
                  ? "Every model OpenRouter currently serves for free. The list is fetched from OpenRouter, so it follows their roster."
                  : "The models this app can both run and cost for the selected provider."
              }
              required
            >
              {(control) => (
                <ModelPicker
                  {...control}
                  value={model}
                  onChange={setModel}
                  options={modelsResult.data?.providerModels ?? []}
                  fetching={modelsResult.fetching}
                  failed={modelsResult.error !== undefined}
                />
              )}
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                label="Temperature"
                description="0 is repeatable, 2 is loose. Some models (claude-opus-5, claude-sonnet-5) reject it and the API drops it for them."
                required
              >
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={temperature}
                    onChange={(e) => setTemperature(Number(e.target.value))}
                  />
                )}
              </Field>

              <Field label="Max tokens" description="Ceiling on one reply's length." required>
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={32000}
                    step={1}
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(Number(e.target.value))}
                  />
                )}
              </Field>
            </div>
          </CardBody>

          {/* In the footer, so a save no longer shifts the form under the
           * cursor the way an inserted banner did. */}
          <CardFooter>
            <Button type="submit" loading={updateAgentResult.fetching} loadingLabel="Saving…">
              Save agent
            </Button>
            {agentSaved ? (
              <span role="status" className="text-sm font-medium text-success">
                Saved
              </span>
            ) : null}
          </CardFooter>
        </Card>
      </form>

      <form onSubmit={onSubmitConfig}>
        <Card>
          <CardHeader
            title="Behaviour"
            description="How the agent speaks, and how hard it works on one answer."
          />
          <CardBody className="space-y-4">
            {configError ? <Alert tone="danger">{configError}</Alert> : null}

            <Field
              label="Tone"
              description="Folded into the system prompt — for example “direct and factual”."
              required
            >
              {(control) => (
                <Input {...control} type="text" value={tone} onChange={(e) => setTone(e.target.value)} />
              )}
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              {/* Saving a value that does nothing yet is fine. Not saying so is not. */}
              <Field
                label="Retrieval top-K"
                description="How many knowledge chunks to retrieve. Takes effect in Phase 3 (retrieval)."
                required
              >
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={50}
                    step={1}
                    value={retrievalTopK}
                    onChange={(e) => setRetrievalTopK(Number(e.target.value))}
                  />
                )}
              </Field>

              <Field
                label="Max agent steps"
                description="Tool-calling rounds per answer. Takes effect in Phase 4 (tools)."
                required
              >
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={20}
                    step={1}
                    value={maxAgentSteps}
                    onChange={(e) => setMaxAgentSteps(Number(e.target.value))}
                  />
                )}
              </Field>
            </div>
          </CardBody>

          <CardFooter>
            <Button type="submit" loading={updateConfigResult.fetching} loadingLabel="Saving…">
              Save behaviour
            </Button>
            {configSaved ? (
              <span role="status" className="text-sm font-medium text-success">
                Saved
              </span>
            ) : null}
          </CardFooter>
        </Card>
      </form>

      <Card tone="danger">
        <CardHeader
          title="Delete this agent"
          description="Its conversations and configuration go with it. This cannot be undone."
        />
        <CardBody className="space-y-3">
          {deleteError ? <Alert tone="danger">{deleteError}</Alert> : null}
          <Button
            variant="danger"
            onClick={onDelete}
            loading={deleteResult.fetching}
            loadingLabel="Deleting…"
          >
            Delete agent
          </Button>
        </CardBody>
      </Card>
    </div>
  );
}
