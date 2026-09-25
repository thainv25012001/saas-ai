"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Select } from "@/components/ui/Input";

export type PromptOption = { id: string; name: string; activeVersion: number | null };

/** The select's value for "no prompt" -- a real choice here, so it is not the
 * empty string DESIGN.md reserves for an unset placeholder. */
const DEFAULT_VALUE = "default";

export function AgentPromptCard({
  prompts,
  fetching,
  failed,
  currentPromptId,
  saving,
  error,
  saved,
  onSave,
}: {
  prompts: readonly PromptOption[];
  fetching: boolean;
  failed: boolean;
  currentPromptId: string | null;
  saving: boolean;
  error: string | null;
  saved: boolean;
  onSave: (promptId: string | null) => void;
}) {
  const [value, setValue] = useState(currentPromptId ?? DEFAULT_VALUE);
  useEffect(() => setValue(currentPromptId ?? DEFAULT_VALUE), [currentPromptId]);

  const chosen = prompts.find((prompt) => prompt.id === value) ?? null;

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    onSave(value === DEFAULT_VALUE ? null : value);
  }

  let control: React.ReactNode;
  if (fetching && prompts.length === 0) {
    control = <p className="text-sm text-ink-muted">Loading prompts…</p>;
  } else if (failed) {
    control = <p className="text-sm text-ink-muted">Could not load prompts. The agent keeps its current prompt.</p>;
  } else {
    control = (
      <Field label="System prompt">
        {(props) => (
          <Select {...props} value={value} onChange={(e) => setValue(e.target.value)} title={chosen?.name}>
            <option value={DEFAULT_VALUE}>Built-in default</option>
            {prompts.map((prompt) => (
              <option key={prompt.id} value={prompt.id}>
                {prompt.name}
              </option>
            ))}
          </Select>
        )}
      </Field>
    );
  }

  return (
    <form onSubmit={onSubmit}>
      <Card>
        <CardHeader title="Prompt" description="The versioned system prompt this agent answers on." />
        <CardBody className="space-y-3">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          {control}
          {!failed && !fetching && prompts.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No prompts yet — <Link href="/dashboard/prompts" className="underline">create one</Link>.
            </p>
          ) : null}
          {!failed && chosen ? (
            <p className="text-sm text-ink-muted">
              {chosen.activeVersion !== null ? `Runs v${chosen.activeVersion}. ` : null}
              <Link href={`/dashboard/prompts/${chosen.id}`} className="underline">Open prompt</Link>
            </p>
          ) : null}
          {!failed && value === DEFAULT_VALUE ? (
            <p className="text-xs text-ink-subtle">
              Answers on the default are not tied to a versioned prompt, so they cannot be pinned in an evaluation.
            </p>
          ) : null}
        </CardBody>
        <CardFooter>
          <Button type="submit" disabled={failed} loading={saving} loadingLabel="Saving…">
            Save prompt
          </Button>
          {saved ? (
            <span role="status" className="text-sm font-medium text-success">
              Saved
            </span>
          ) : null}
        </CardFooter>
      </Card>
    </form>
  );
}
