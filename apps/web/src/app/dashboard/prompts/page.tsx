"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { CreatePromptForm, type NewPrompt } from "@/components/prompts/CreatePromptForm";
import { PromptList } from "@/components/prompts/PromptList";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { CreatePromptDocument, DefaultSystemPromptDocument, PromptsDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { activeVersionOf } from "@/lib/prompts";

export default function PromptsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [creating, setCreating] = useState(false);

  const [{ data, fetching, error }] = useQuery({
    query: PromptsDocument,
    pause: loading || !user,
    requestPolicy: "cache-and-network",
  });
  const [defaultResult] = useQuery({ query: DefaultSystemPromptDocument, pause: loading || !user });
  const [createResult, createPrompt] = useMutation(CreatePromptDocument);

  const rows = useMemo(
    () =>
      (data?.prompts ?? []).map((prompt) => ({
        id: prompt.id,
        name: prompt.name,
        key: prompt.key,
        activeVersion: activeVersionOf(prompt.versions)?.version ?? null,
        versionCount: prompt.versions.length,
        agentCount: prompt.agents.length,
      })),
    [data],
  );

  async function onCreate(values: NewPrompt) {
    const result = await createPrompt({ input: values });
    const id = result.data?.createPrompt.id;
    // Straight to the prompt: linking it to an agent and drafting v2 happen there.
    if (id) router.push(`/dashboard/prompts/${id}`);
  }

  const form = (
    <CreatePromptForm
      defaultPrompt={defaultResult.data?.defaultSystemPrompt ?? null}
      submitting={createResult.fetching}
      error={firstGraphQLError(createResult.error)}
      onCreate={onCreate}
    />
  );

  const queryError = firstGraphQLError(error);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Prompts"
        description="The versioned system prompts behind your agents."
        actions={
          rows.length > 0 && !creating ? (
            <Button onClick={() => setCreating(true)}>New prompt</Button>
          ) : null
        }
      />
      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}
      {fetching && !data ? (
        <LoadingState label="Loading prompts…" />
      ) : rows.length === 0 ? (
        <>
          <Card>
            <EmptyState
              icon="prompt"
              title="No prompts yet"
              description="Agents without a prompt answer on the built-in default. Create a prompt to version that text and pin it in evaluations."
            />
          </Card>
          {form}
        </>
      ) : (
        <>
          {creating ? form : null}
          <Card>
            <PromptList prompts={rows} />
          </Card>
        </>
      )}
    </div>
  );
}
