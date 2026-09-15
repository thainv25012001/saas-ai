"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery } from "urql";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Icon } from "@/components/ui/icons";
import { Input } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument, CreateAgentDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";

export default function AgentsPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching, error }, refetchAgents] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const [createResult, createAgent] = useMutation(CreateAgentDocument);
  const [name, setName] = useState("");
  // The list is what you came for, so the create form is disclosed rather
  // than parked above it permanently.
  const [creating, setCreating] = useState(false);

  const agents = data?.agents ?? [];
  const createError = firstGraphQLError(createResult.error);
  const queryError = firstGraphQLError(error);

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    const result = await createAgent({ name });
    if (!result.error) {
      setName("");
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
            <Button onClick={() => setCreating(true)}>
              <Icon name="plus" size="md" />
              New agent
            </Button>
          )
        }
      />

      {creating ? (
        <Card>
          <form onSubmit={onCreate} className="space-y-4 p-5">
            <Field
              label="Agent name"
              description="Used to generate the agent's slug. You can change the name later."
              error={createError}
              required
            >
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              )}
            </Field>
            <div className="flex items-center gap-2">
              <Button type="submit" loading={createResult.fetching} loadingLabel="Creating…">
                Create agent
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setCreating(false);
                  setName("");
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
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
            action={<Button onClick={() => setCreating(true)}>Create your first agent</Button>}
          />
        ) : (
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
        )}
      </Card>
    </div>
  );
}
