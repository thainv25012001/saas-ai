"use client";

import Link from "next/link";
import { useQuery } from "urql";
import { ButtonLink } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { checklistProgress, deriveChecklist } from "@/lib/setup-checklist";

const PHASES_AHEAD = [
  "Phase 3 — Knowledge: upload documents and let the agent answer from them.",
  "Phase 4 — Products, tools and lead capture.",
  "Phase 5 — Evaluation, MCP and billing.",
];

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching, error }] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });

  const agents = data?.agents ?? [];
  const steps = deriveChecklist(agents);
  const progress = checklistProgress(steps);
  const activeCount = agents.filter((agent) => agent.status === "ACTIVE").length;
  const draftCount = agents.filter((agent) => agent.status === "DRAFT").length;

  if (fetching && !data) return <LoadingState label="Loading your workspace…" />;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description={`What ${user?.organization_name ?? "your workspace"} has set up, and what is left to do.`}
      />

      {error ? (
        <Card>
          <CardBody>
            <p className="text-sm text-danger">
              Could not load your agents. Reload the page to try again.
            </p>
          </CardBody>
        </Card>
      ) : null}

      <Card>
        <CardHeader
          title="Get your assistant ready"
          description={`${progress.done} of ${progress.total} steps done.`}
        />
        <ul className="divide-y divide-line">
          {steps.map((step) => (
            <li key={step.id} className="flex items-start gap-3 px-5 py-4">
              <span
                className={
                  step.done === true
                    ? "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-control bg-success-surface text-success"
                    : "mt-0.5 flex size-5 shrink-0 items-center justify-center text-ink-subtle"
                }
              >
                <Icon name={step.done === true ? "check" : "circle"} size="md" />
              </span>
              <div className="min-w-0 flex-1">
                <p
                  className={
                    step.done === true
                      ? "text-sm font-medium text-ink-muted line-through"
                      : "text-sm font-medium text-ink"
                  }
                >
                  {step.title}
                </p>
                <p className="mt-0.5 text-sm text-ink-muted">{step.description}</p>
              </div>
              {step.done === true ? null : (
                <ButtonLink href={step.action.href} variant="secondary" size="sm" className="shrink-0">
                  {step.action.label}
                </ButtonLink>
              )}
            </li>
          ))}
        </ul>
      </Card>

      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: "Agents", value: agents.length },
          { label: "Active", value: activeCount },
          { label: "Draft", value: draftCount },
        ].map((tile) => (
          <Card key={tile.label}>
            <CardBody>
              <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
                {tile.label}
              </p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-ink">{tile.value}</p>
            </CardBody>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader
          title="Your agents"
          actions={
            <ButtonLink href="/dashboard/agents" variant="secondary" size="sm">
              All agents
            </ButtonLink>
          }
        />
        {agents.length === 0 ? (
          <EmptyState
            icon="agent"
            title="No agents yet"
            description="An agent is one assistant, with its own model, prompt and behaviour."
            action={<ButtonLink href="/dashboard/agents">Create your first agent</ButtonLink>}
          />
        ) : (
          <ul className="divide-y divide-line">
            {agents.slice(0, 5).map((agent) => (
              <li key={String(agent.id)} className="flex items-center gap-3 px-5 py-3">
                <div className="min-w-0 flex-1">
                  <Link
                    href={`/dashboard/agents/${String(agent.id)}`}
                    className="text-sm font-medium text-ink hover:underline"
                  >
                    {agent.name}
                  </Link>
                  <p className="truncate text-xs text-ink-subtle">
                    {agent.provider} · {agent.model}
                  </p>
                </div>
                <Badge tone={agentStatusTone(agent.status)}>{agentStatusLabel(agent.status)}</Badge>
                <Link
                  href={`/dashboard/playground?agentId=${String(agent.id)}`}
                  className="shrink-0 text-sm text-ink-muted hover:text-ink hover:underline"
                >
                  Test
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardHeader title="What's next" description="Sections the navigation already shows." />
        <CardBody>
          <ul className="space-y-1.5 text-sm text-ink-muted">
            {PHASES_AHEAD.map((phase) => (
              <li key={phase}>{phase}</li>
            ))}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}
