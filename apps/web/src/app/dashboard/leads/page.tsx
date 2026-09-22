"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "urql";
import { LeadsTable } from "@/components/leads/LeadsTable";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ButtonLink } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { Select } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument, LeadsDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";

export default function LeadsPage() {
  const { user, loading } = useAuth();

  const [{ data: agentsData, fetching: agentsFetching, error: agentsError }] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const agents = useMemo(() => agentsData?.agents ?? [], [agentsData]);

  const [agentId, setAgentId] = useState<string | null>(null);

  // Same shape as the playground's own agent picker: nothing is selected
  // until the list resolves, and this only ever runs while nothing has been
  // chosen yet, so it never fights a deliberate switch from the dropdown.
  useEffect(() => {
    if (agentId !== null) return;
    if (agents.length > 0) setAgentId(String(agents[0].id));
  }, [agentId, agents]);

  const [{ data, fetching, error }] = useQuery({
    query: LeadsDocument,
    variables: { agentId: agentId ?? "" },
    pause: agentId === null,
  });

  const leads = useMemo(
    () =>
      (data?.leads ?? []).map((row) => ({
        id: String(row.id),
        name: row.name,
        email: row.email,
        phone: row.phone,
        interest: row.interest,
        status: row.status,
        createdAt: String(row.createdAt),
        conversation: row.conversation
          ? {
              id: String(row.conversation.id),
              title: row.conversation.title,
              preview: row.conversation.preview,
            }
          : null,
      })),
    [data],
  );

  if (loading || (agentsFetching && !agentsData)) {
    return <LoadingState label="Loading leads…" />;
  }

  if (agents.length === 0) {
    return (
      <div className="mx-auto w-full max-w-6xl px-6 py-8">
        <PageHeader
          title="Leads"
          description="The customers your assistant captured, and what they asked for."
        />
        {agentsError ? (
          <Alert tone="danger">{firstGraphQLError(agentsError)}</Alert>
        ) : (
          <EmptyState
            icon="lead"
            title="No agents yet"
            description="Leads are captured per agent, by the create_lead tool. Create an agent first, then turn the tool on from its Tools card."
            action={<ButtonLink href="/dashboard/agents">Create an agent</ButtonLink>}
          />
        )}
      </div>
    );
  }

  const queryError = firstGraphQLError(error);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Leads"
        description="The customers your assistant captured, and what they asked for. Read-only for now."
        meta={
          <label className="flex items-center gap-2 text-sm text-ink-muted">
            <span className="font-medium">Agent</span>
            <Select
              value={agentId ?? ""}
              onChange={(e) => setAgentId(e.target.value)}
              width="auto"
              className="min-w-44"
            >
              {agents.map((agent) => (
                <option key={String(agent.id)} value={String(agent.id)}>
                  {agent.name}
                </option>
              ))}
            </Select>
          </label>
        }
      />

      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}

      <Card>
        {fetching && !data ? (
          <LoadingState label="Loading leads…" />
        ) : queryError ? null : (
          <LeadsTable leads={leads} />
        )}
      </Card>
    </div>
  );
}
