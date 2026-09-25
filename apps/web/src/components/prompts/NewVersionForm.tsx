"use client";

import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { PROMPT_VARIABLES_HELP } from "./CreatePromptForm";

/** Remount it (`key`) when the base version changes -- its state is seeded
 * from `baseText` once. */
export function NewVersionForm({
  baseText,
  baseVersion,
  submitting,
  error,
  onSave,
}: {
  baseText: string;
  baseVersion: number;
  submitting: boolean;
  error: string | null;
  onSave: (values: { systemPrompt: string; notes: string | null }) => void;
}) {
  const [text, setText] = useState(baseText);
  const [notes, setNotes] = useState("");
  const unchanged = text === baseText || text.trim() === "";

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (unchanged) return;
    onSave({ systemPrompt: text, notes: notes.trim() || null });
  }

  return (
    <form onSubmit={onSubmit}>
      <Card>
        <CardHeader
          title="New version"
          description={`Starts from v${baseVersion}. Saved as a draft: nothing changes for customers until you activate it.`}
        />
        <CardBody className="space-y-4">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <Field label="System prompt" description={PROMPT_VARIABLES_HELP} required>
            {(control) => (
              <Textarea {...control} rows={14} className="font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} />
            )}
          </Field>
          <Field label="Notes" description="What changed, for whoever reads the history next.">
            {(control) => <Input {...control} type="text" value={notes} onChange={(e) => setNotes(e.target.value)} />}
          </Field>
        </CardBody>
        <CardFooter>
          <Button type="submit" disabled={unchanged} loading={submitting} loadingLabel="Saving…">
            Save draft
          </Button>
        </CardFooter>
      </Card>
    </form>
  );
}
