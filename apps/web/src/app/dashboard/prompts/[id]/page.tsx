"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { useMutation, useQuery } from "urql";
import { NewVersionForm } from "@/components/prompts/NewVersionForm";
import { VersionList } from "@/components/prompts/VersionList";
import { VersionView } from "@/components/prompts/VersionView";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { ActivatePromptVersionDocument, CreatePromptVersionDocument, PromptDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { activeVersionOf } from "@/lib/prompts";

export default function PromptDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { user, loading } = useAuth();
  const [{ data, fetching, error }, refetch] = useQuery({
    query: PromptDocument,
    variables: { id },
    pause: loading || !user,
  });
  const [activateResult, activate] = useMutation(ActivatePromptVersionDocument);
  const [createResult, createVersion] = useMutation(CreatePromptVersionDocument);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftSaved, setDraftSaved] = useState<number | null>(null);

  const prompt = data?.prompt;
  const versions = prompt?.versions ?? [];
  const active = activeVersionOf(versions);
  const selected = versions.find((v) => v.id === selectedId) ?? active ?? versions[0] ?? null;

  // Select the active version once the prompt has loaded.
  useEffect(() => {
    if (selectedId === null && active) setSelectedId(active.id);
  }, [active, selectedId]);

  async function onActivate(versionId: string) {
    const result = await activate({ versionId });
    if (!result.error) {
      setDraftSaved(null);
      refetch({ requestPolicy: "network-only" });
    }
  }

  async function onSave(values: { systemPrompt: string; notes: string | null }) {
    const result = await createVersion({ promptId: id, input: values });
    const created = result.data?.createPromptVersion;
    if (created) {
      setSelectedId(created.id);
      setDraftSaved(created.version);
      refetch({ requestPolicy: "network-only" });
    }
  }

  if (fetching && !data) return <LoadingState label="Loading prompt…" />;
  const queryError = firstGraphQLError(error);
  if (!prompt) return queryError ? <Alert tone="danger">{queryError}</Alert> : null;

  const agentNames = prompt.agents.map((agent) => agent.name);

  return (
    <div className="space-y-4">
      <PageHeader
        title={prompt.name}
        description={prompt.description ?? undefined}
        breadcrumb={[{ href: "/dashboard/prompts", label: "Prompts" }]}
        meta={
          <>
            <span className="font-mono text-xs text-ink-subtle">{prompt.key}</span>
            {active ? <Badge tone="success">{`Active v${active.version}`}</Badge> : null}
          </>
        }
      />
      {draftSaved !== null ? (
        <Alert tone="success">
          {`Saved v${draftSaved} as a draft. It is not live until you activate it — `}
          <Link href="/dashboard/evaluations" className="underline">run an evaluation</Link>
          {" first to compare it with the active version."}
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <Card className="self-start">
          <CardHeader title="Versions" description="Newest first." />
          <VersionList versions={versions} selectedId={selected?.id ?? null} onSelect={setSelectedId} />
        </Card>
        {selected ? (
          <VersionView
            version={selected}
            activeVersion={active?.version ?? null}
            agentNames={agentNames}
            activating={activateResult.fetching}
            error={firstGraphQLError(activateResult.error)}
            onActivate={onActivate}
          />
        ) : null}
      </div>

      {selected ? (
        <NewVersionForm
          key={selected.id}
          baseText={selected.systemPrompt}
          baseVersion={selected.version}
          submitting={createResult.fetching}
          error={firstGraphQLError(createResult.error)}
          onSave={onSave}
        />
      ) : null}

      <Card>
        <CardHeader title="Used by" description="Each agent here answers on the active version." />
        <CardBody>
          {prompt.agents.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No agents use this prompt yet. Choose it in an agent’s Prompt card.
            </p>
          ) : (
            <ul className="divide-y divide-line">
              {prompt.agents.map((agent) => (
                <li key={agent.id} className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0">
                  <Link href={`/dashboard/agents/${agent.id}`} className="text-sm text-ink hover:underline">
                    {agent.name}
                  </Link>
                  <Badge tone={agentStatusTone(agent.status)}>{agentStatusLabel(agent.status)}</Badge>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
