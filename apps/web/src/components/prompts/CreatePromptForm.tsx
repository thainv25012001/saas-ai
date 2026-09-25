"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { PROMPT_KEY_PATTERN, promptKeyFromName } from "@/lib/prompts";

export type NewPrompt = { name: string; key: string; description: string | null; systemPrompt: string };

export const PROMPT_VARIABLES_HELP =
  "{{company_name}} and {{agent_name}} are filled in when the agent answers.";

export function CreatePromptForm({
  defaultPrompt,
  submitting,
  error,
  onCreate,
}: {
  /** The built-in default text; `null` while it loads. */
  defaultPrompt: string | null;
  submitting: boolean;
  error: string | null;
  onCreate: (values: NewPrompt) => void;
}) {
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [keyEdited, setKeyEdited] = useState(false);
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState(defaultPrompt ?? "");
  const [keyError, setKeyError] = useState<string | null>(null);

  // The default arrives after first render; seed it once, and never over
  // something the user has typed.
  useEffect(() => {
    if (defaultPrompt !== null) setSystemPrompt((current) => (current === "" ? defaultPrompt : current));
  }, [defaultPrompt]);

  function onNameChange(value: string) {
    setName(value);
    if (!keyEdited) setKey(promptKeyFromName(value));
  }

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!PROMPT_KEY_PATTERN.test(key)) {
      setKeyError("Use lowercase letters, digits and underscores only.");
      return;
    }
    setKeyError(null);
    onCreate({ name: name.trim(), key, description: description.trim() || null, systemPrompt });
  }

  return (
    <form onSubmit={onSubmit}>
      <Card>
        <CardHeader title="New prompt" description="Version 1 is created and made active straight away." />
        <CardBody className="space-y-4">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Name" required>
              {(control) => (
                <Input {...control} type="text" required maxLength={255} value={name} onChange={(e) => onNameChange(e.target.value)} />
              )}
            </Field>
            <Field label="Key" description="A stable identifier, e.g. sales_system." error={keyError ?? undefined} required>
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  required
                  maxLength={100}
                  className="font-mono"
                  value={key}
                  onChange={(e) => {
                    setKeyEdited(true);
                    setKey(e.target.value);
                  }}
                />
              )}
            </Field>
          </div>
          <Field label="Description">
            {(control) => (
              <Input {...control} type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
            )}
          </Field>
          <Field label="System prompt" description={PROMPT_VARIABLES_HELP} required>
            {(control) => (
              <Textarea
                {...control}
                required
                rows={14}
                className="font-mono text-xs"
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
              />
            )}
          </Field>
        </CardBody>
        <CardFooter>
          <Button type="submit" loading={submitting} loadingLabel="Creating…">
            Create prompt
          </Button>
        </CardFooter>
      </Card>
    </form>
  );
}
