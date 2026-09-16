"use client";

import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input, Select } from "@/components/ui/Input";
import { ModelPicker, type ModelChoice } from "@/components/agents/ModelPicker";
import { creatableProviders, modelFieldHelp, providerLabel } from "@/lib/providers";
import type { ProviderInfo } from "@/lib/providers";

export type CreateAgentValues = {
  name: string;
  provider: string;
  model: string;
};

export type CreateAgentFormProps = {
  /** Every provider the API reports, unfiltered — the form decides what to offer. */
  providers: readonly ProviderInfo[];
  /** The selected provider. Owned by the page, because the model query keys on it. */
  provider: string;
  onProviderChange: (provider: string) => void;
  models: readonly ModelChoice[];
  modelsFetching?: boolean;
  modelsFailed?: boolean;
  submitting?: boolean;
  /** The API's own message from a failed create, or `null`. */
  error?: string | null;
  onSubmit: (values: CreateAgentValues) => void;
  onCancel: () => void;
};

/**
 * The new-agent form.
 *
 * It asks for a provider and a model, not just a name. It used to ask only for
 * a name, and the API filled the rest in from `DEFAULT_LLM_PROVIDER` — `fake`
 * out of the box — so every agent anyone created started on the offline
 * provider that answers with a canned reply, and they had to discover that and
 * fix it on the agent's detail page.
 *
 * Presentational on purpose: the page owns the two GraphQL queries and the
 * mutation, this owns the form. That keeps it testable without a urql client,
 * the same way `ModelPicker` is.
 */
export function CreateAgentForm({
  providers,
  provider,
  onProviderChange,
  models,
  modelsFetching = false,
  modelsFailed = false,
  submitting = false,
  error = null,
  onSubmit,
  onCancel,
}: CreateAgentFormProps) {
  const [name, setName] = useState("");
  const [model, setModel] = useState("");

  const options = creatableProviders(providers);
  const noProviders = options.length === 0;
  // A name and a model are both required by the API. Disabled rather than
  // submitted-and-rejected: the model picker starts empty, so an eager click
  // would otherwise send "" and come back as a validation error for something
  // the form could see for itself.
  const canSubmit = name.trim() !== "" && model !== "" && !noProviders && !submitting;

  function changeProvider(next: string) {
    // A model id belongs to one provider. Carrying the old one over would
    // create an agent whose model its provider has never heard of.
    setModel("");
    onProviderChange(next);
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    onSubmit({ name: name.trim(), provider, model });
  }

  return (
    <form onSubmit={submit} className="space-y-4 p-5">
      {error ? <Alert tone="danger">{error}</Alert> : null}

      {noProviders ? (
        <Alert tone="danger" title="No model provider is configured">
          Set OPENAI_API_KEY, ANTHROPIC_API_KEY or OPENROUTER_API_KEY on the API and restart it.
          Until one is set there is no provider an agent can answer with.
        </Alert>
      ) : null}

      <Field
        label="Agent name"
        description="Used to generate the agent's slug. You can change the name later."
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

      <Field
        label="Provider"
        description="Only providers with an API key configured on the server are listed. Changing it clears the model below."
        required
      >
        {(control) => (
          <Select
            {...control}
            value={provider}
            disabled={noProviders}
            onChange={(e) => changeProvider(e.target.value)}
          >
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {providerLabel(option.id)}
              </option>
            ))}
          </Select>
        )}
      </Field>

      <Field label="Model" description={modelFieldHelp(provider)} required>
        {(control) => (
          <ModelPicker
            {...control}
            value={model}
            onChange={setModel}
            options={models}
            fetching={modelsFetching}
            failed={modelsFailed}
          />
        )}
      </Field>

      <div className="flex items-center gap-2">
        <Button type="submit" disabled={!canSubmit} loading={submitting} loadingLabel="Creating…">
          Create agent
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
