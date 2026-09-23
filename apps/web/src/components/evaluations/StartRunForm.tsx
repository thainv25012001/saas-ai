"use client";

import { useState } from "react";
import { type ModelChoice, ModelPicker } from "@/components/agents/ModelPicker";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { cn, focusRing } from "@/components/ui/cn";
import { Field } from "@/components/ui/Field";
import { Select } from "@/components/ui/Input";
import { buildStartRunPayload, runCallEstimate, type StartRunPayload } from "@/lib/evaluations";
import { type ProviderInfo, providerLabel } from "@/lib/providers";

export type RunAgent = { id: string; name: string; provider: string; model: string };
export type PromptVersionOption = { id: string; version: number; isActive: boolean };

export type ModelsState = { options: readonly ModelChoice[]; fetching: boolean; failed: boolean };
export type VersionsState = {
  versions: readonly PromptVersionOption[];
  fetching: boolean;
  failed: boolean;
  /** The agent has no prompt of its own, so there is no version to pin. */
  noPrompt: boolean;
};

const checkboxClasses = cn("size-4 rounded-control accent-primary", focusRing, "focus-visible:ring-offset-1");

/**
 * Configures and starts one run of a dataset. The page owns the queries,
 * which reach this form as two hooks -- `useModels(provider)` for each
 * `ModelPicker` and `useVersions(agentId)` for the prompt versions -- so the
 * form keeps its own choices without the page mirroring every field.
 *
 * The prompt version is always sent explicitly (the active one by default):
 * the server would pin the active version anyway, but naming it means the
 * version shown here is the one that runs, even if another is activated
 * between opening the form and pressing Start.
 */
export function StartRunForm({
  datasetId,
  caseCount,
  agents,
  providers,
  useModels,
  useVersions,
  onStart,
  starting = false,
  error = null,
  blockedReason = null,
}: {
  datasetId: string;
  caseCount: number;
  agents: readonly RunAgent[];
  providers: readonly ProviderInfo[];
  useModels: (provider: string) => ModelsState;
  useVersions: (agentId: string) => VersionsState;
  onStart: (payload: StartRunPayload) => void | Promise<void>;
  starting?: boolean;
  error?: string | null;
  /** Why a run cannot start right now (one is already in progress). */
  blockedReason?: string | null;
}) {
  const [agentId, setAgentId] = useState(agents.length === 1 ? agents[0].id : "");
  const [versionChoice, setVersionChoice] = useState<string | null>(null);
  const [overrideOn, setOverrideOn] = useState(false);
  const [overrideProvider, setOverrideProvider] = useState("");
  const [overrideModel, setOverrideModel] = useState("");
  const [judgeOn, setJudgeOn] = useState(false);
  const [judgeProvider, setJudgeProvider] = useState("");
  const [judgeModel, setJudgeModel] = useState("");

  const agent = agents.find((candidate) => candidate.id === agentId) ?? null;
  const versions = useVersions(agentId);
  const overrideModels = useModels(overrideOn ? overrideProvider : "");
  const judgeModels = useModels(judgeOn ? judgeProvider : "");

  // What the version select holds: the user's pick while it is still one of
  // this agent's versions, otherwise the active one.
  const active = versions.versions.find((version) => version.isActive) ?? null;
  const versionId =
    versionChoice && versions.versions.some((version) => version.id === versionChoice)
      ? versionChoice
      : (active?.id ?? versions.versions[0]?.id ?? "");

  const calls = runCallEstimate(caseCount, judgeOn);
  const overrideIncomplete = overrideOn && (!overrideProvider || !overrideModel);
  const judgeIncomplete = judgeOn && (!judgeProvider || !judgeModel);
  const versionPending = !versions.noPrompt && !versions.failed && versionId === "";
  const canStart =
    agent !== null &&
    caseCount > 0 &&
    !blockedReason &&
    !overrideIncomplete &&
    !judgeIncomplete &&
    !versions.fetching &&
    !versionPending;

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canStart || !agent) return;
    void onStart(
      buildStartRunPayload({
        datasetId,
        agentId: agent.id,
        promptVersionId: versions.noPrompt ? null : versionId || null,
        override: overrideOn ? { provider: overrideProvider, model: overrideModel } : null,
        judge: judgeOn ? { provider: judgeProvider, model: judgeModel } : null,
      }),
    );
  }

  function providerOptions() {
    return providers.map((option) => (
      <option key={option.id} value={option.id} disabled={!option.configured}>
        {option.configured ? providerLabel(option.id) : `${providerLabel(option.id)} — no API key`}
      </option>
    ));
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      {error ? <Alert tone="danger">{error}</Alert> : null}
      {blockedReason ? <Alert tone="info">{blockedReason}</Alert> : null}

      <Field label="Agent" required>
        {(control) => (
          <Select
            {...control}
            value={agentId}
            onChange={(event) => {
              setAgentId(event.target.value);
              setVersionChoice(null);
            }}
          >
            {agentId === "" ? (
              <option value="" disabled>
                {agents.length === 0 ? "No agents yet" : "Select an agent"}
              </option>
            ) : null}
            {agents.map((option) => (
              <option key={option.id} value={option.id}>
                {option.name}
              </option>
            ))}
          </Select>
        )}
      </Field>

      {agent ? (
        <Field
          label="Prompt version"
          description="Draft a version, run it here, and activate it only if it scores better."
        >
          {(control) =>
            versions.noPrompt ? (
              <p className="text-sm text-ink-muted">This agent has no prompt of its own; it runs on the default.</p>
            ) : versions.failed ? (
              <p className="text-sm text-danger">
                Could not load this agent’s prompt versions. The run will use the active one.
              </p>
            ) : (
              <Select
                {...control}
                value={versionId}
                disabled={versions.fetching}
                onChange={(event) => setVersionChoice(event.target.value)}
              >
                {versionId === "" ? (
                  <option value="" disabled>
                    {versions.fetching ? "Loading versions…" : "No versions"}
                  </option>
                ) : null}
                {versions.versions.map((version) => (
                  <option key={version.id} value={version.id}>
                    {version.isActive ? `v${version.version} (active)` : `v${version.version}`}
                  </option>
                ))}
              </Select>
            )
          }
        </Field>
      ) : null}

      <div className="space-y-3 rounded-card border border-line p-4">
        <label className="flex items-center gap-2 text-sm font-medium text-ink">
          <input
            type="checkbox"
            className={checkboxClasses}
            checked={overrideOn}
            onChange={(event) => {
              setOverrideOn(event.target.checked);
              if (event.target.checked && agent) {
                setOverrideProvider(agent.provider);
                setOverrideModel(agent.model);
              }
            }}
          />
          Use a different model for this run
        </label>
        {overrideOn ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Provider">
              {(control) => (
                <Select
                  {...control}
                  value={overrideProvider}
                  onChange={(event) => {
                    setOverrideProvider(event.target.value);
                    setOverrideModel("");
                  }}
                >
                  {overrideProvider === "" ? (
                    <option value="" disabled>
                      Select a provider
                    </option>
                  ) : null}
                  {providerOptions()}
                </Select>
              )}
            </Field>
            <Field label="Model">
              {(control) => (
                <ModelPicker
                  {...control}
                  value={overrideModel}
                  onChange={setOverrideModel}
                  options={overrideModels.options}
                  fetching={overrideModels.fetching}
                  failed={overrideModels.failed}
                />
              )}
            </Field>
          </div>
        ) : agent ? (
          <p className="text-xs text-ink-subtle">
            Runs on the agent’s own model: <span className="font-mono">{agent.model}</span>
          </p>
        ) : null}
      </div>

      <div className="space-y-3 rounded-card border border-line p-4">
        <label className="flex items-center gap-2 text-sm font-medium text-ink">
          <input
            type="checkbox"
            className={checkboxClasses}
            checked={judgeOn}
            onChange={(event) => setJudgeOn(event.target.checked)}
          />
          Grade answers with an LLM judge
        </label>
        {judgeOn ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Judge provider">
              {(control) => (
                <Select
                  {...control}
                  value={judgeProvider}
                  onChange={(event) => {
                    setJudgeProvider(event.target.value);
                    setJudgeModel("");
                  }}
                >
                  {judgeProvider === "" ? (
                    <option value="" disabled>
                      Select a provider
                    </option>
                  ) : null}
                  {providerOptions()}
                </Select>
              )}
            </Field>
            <Field label="Judge model">
              {(control) => (
                <ModelPicker
                  {...control}
                  value={judgeModel}
                  onChange={setJudgeModel}
                  options={judgeModels.options}
                  fetching={judgeModels.fetching}
                  failed={judgeModels.failed}
                />
              )}
            </Field>
          </div>
        ) : (
          <p className="text-xs text-ink-subtle">
            Without a judge, reference answers are not graded; the other expectations still are.
          </p>
        )}
      </div>

      <p className="text-sm text-ink" aria-live="polite">
        {`${caseCount} ${caseCount === 1 ? "case" : "cases"} × ${judgeOn ? 2 : 1} LLM ${judgeOn ? "calls" : "call"} = ${calls} ${calls === 1 ? "call" : "calls"}`}
        <span className="block text-xs text-ink-subtle">
          {judgeOn ? "The judge grades only cases with a reference answer. " : ""}A turn that uses tools makes
          more than one call, so this is a floor. Calls are billed at the provider’s normal rate.
        </span>
      </p>

      <Button type="submit" disabled={!canStart} loading={starting} loadingLabel="Starting…">
        Start run
      </Button>
    </form>
  );
}
