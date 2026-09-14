"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "urql";
import {
  AgentDocument,
  AgentStatus,
  DeleteAgentDocument,
  UpdateAgentConfigDocument,
  UpdateAgentDocument,
} from "@/graphql/generated";
import { useAuth } from "@/lib/auth";

const STATUSES: AgentStatus[] = ["DRAFT", "ACTIVE", "DISABLED"];
const PROVIDERS = ["openai", "anthropic"];

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
  const [provider, setProvider] = useState(PROVIDERS[0]);
  const [model, setModel] = useState("");
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

  const agentError = updateAgentResult.error?.graphQLErrors[0]?.message ?? null;
  const configError = updateConfigResult.error?.graphQLErrors[0]?.message ?? null;
  const deleteError = deleteResult.error?.graphQLErrors[0]?.message ?? null;

  if (fetching && !agent) {
    return <p className="text-slate-500">Loading…</p>;
  }

  if (error && !agent) {
    return (
      <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
        {error.graphQLErrors[0]?.message ?? "Failed to load agent."}
      </p>
    );
  }

  if (!agent) return null;

  return (
    <section className="max-w-2xl space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">{agent.name}</h1>
        <p className="text-sm text-slate-500">{agent.slug}</p>
      </div>

      <form
        onSubmit={onSubmitAgent}
        className="space-y-4 rounded-xl border border-slate-200 bg-white p-6"
      >
        <h2 className="text-lg font-semibold">Agent</h2>

        {agentError && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            {agentError}
          </p>
        )}
        {agentSaved && (
          <p role="status" className="rounded-md bg-green-50 p-3 text-sm text-green-700">
            Saved
          </p>
        )}

        <label className="block text-sm font-medium">
          Name
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm font-medium">
          Status
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as AgentStatus)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          >
            {STATUSES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label className="block text-sm font-medium">
          Provider
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          >
            {PROVIDERS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label className="block text-sm font-medium">
          Model
          <input
            type="text"
            required
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm font-medium">
          Temperature
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            required
            value={temperature}
            onChange={(e) => setTemperature(Number(e.target.value))}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm font-medium">
          Max tokens
          <input
            type="number"
            min={1}
            max={32000}
            step={1}
            required
            value={maxTokens}
            onChange={(e) => setMaxTokens(Number(e.target.value))}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <button
          type="submit"
          disabled={updateAgentResult.fetching}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          {updateAgentResult.fetching ? "Saving…" : "Save agent"}
        </button>
      </form>

      <form
        onSubmit={onSubmitConfig}
        className="space-y-4 rounded-xl border border-slate-200 bg-white p-6"
      >
        <h2 className="text-lg font-semibold">Behaviour</h2>

        {configError && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            {configError}
          </p>
        )}
        {configSaved && (
          <p role="status" className="rounded-md bg-green-50 p-3 text-sm text-green-700">
            Saved
          </p>
        )}

        <label className="block text-sm font-medium">
          Tone
          <input
            type="text"
            required
            value={tone}
            onChange={(e) => setTone(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm font-medium">
          Retrieval top-K
          <input
            type="number"
            min={1}
            max={50}
            step={1}
            required
            value={retrievalTopK}
            onChange={(e) => setRetrievalTopK(Number(e.target.value))}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm font-medium">
          Max agent steps
          <input
            type="number"
            min={1}
            max={20}
            step={1}
            required
            value={maxAgentSteps}
            onChange={(e) => setMaxAgentSteps(Number(e.target.value))}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <button
          type="submit"
          disabled={updateConfigResult.fetching}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          {updateConfigResult.fetching ? "Saving…" : "Save behaviour"}
        </button>
      </form>

      <div className="rounded-xl border border-red-200 bg-white p-6">
        <h2 className="text-lg font-semibold text-red-700">Danger zone</h2>
        {deleteError && (
          <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-700">
            {deleteError}
          </p>
        )}
        <button
          onClick={onDelete}
          disabled={deleteResult.fetching}
          className="mt-3 rounded-md border border-red-300 px-4 py-2 text-sm text-red-700 disabled:opacity-50"
        >
          {deleteResult.fetching ? "Deleting…" : "Delete agent"}
        </button>
      </div>
    </section>
  );
}
