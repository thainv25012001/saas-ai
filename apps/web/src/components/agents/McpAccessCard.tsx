"use client";

import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import { claudeMcpAddCommand, copyToClipboard, mcpJsonConfig } from "@/lib/mcp";
import { formatTimestamp } from "@/lib/format";

export type ApiKeyRow = {
  id: string;
  name: string;
  keyPrefix: string;
  createdByName: string | null;
  createdAt: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
};

/** What `onCreate` hands back on success, so the reveal panel below can show
 * the token against the name it was created under. */
export type CreatedKey = {
  name: string;
  token: string;
};

/**
 * The agent page's MCP access card (docs/PHASE-7.md §6): the endpoint a
 * client points at, the tools this agent actually exposes over it, the
 * key list, and a create form.
 *
 * Presentational like `AgentToolsCard`: the page owns the queries and
 * mutations, this owns rendering and the one piece of state that must never
 * leak into the page -- the just-created token. `onCreate` returns it (or
 * `null` on a failure the page has already turned into `createError`)
 * instead of the page holding it, so nothing outside this component's own
 * `useState` ever has a reference to it, and it is gone the moment
 * `handleDismiss` runs or the component unmounts.
 */
export function McpAccessCard({
  endpointUrl,
  exposedToolNames,
  toolsFetching = false,
  agentDisabled = false,
  keys,
  keysFetching = false,
  canManageKeys,
  creating = false,
  createError = null,
  revokingId = null,
  revokeError = null,
  onCreate,
  onRevoke,
}: {
  endpointUrl: string;
  exposedToolNames: readonly string[];
  /** True while `agentMcpInfo` is (re)loading -- including the refetch a
   * Tools card toggle triggers, so the list never shows a stale set while a
   * fresher one is on the way. */
  toolsFetching?: boolean;
  /** The agent's saved status is DISABLED: `/mcp` refuses its keys with 401
   * until it is re-enabled, so the card says so rather than letting a
   * working-looking key fail with no explanation. */
  agentDisabled?: boolean;
  keys: readonly ApiKeyRow[];
  keysFetching?: boolean;
  /** `false` hides the create form and every Revoke button. A member still
   * sees the endpoint, the exposed tools and the key list -- the same
   * visibility rule `ApiKeyService.list_for_agent` applies server-side. */
  canManageKeys: boolean;
  creating?: boolean;
  createError?: string | null;
  /** The key id currently mid-revoke, so only that row's button shows a
   * loading state, matching `AgentToolsCard`'s `togglingId`. */
  revokingId?: string | null;
  revokeError?: string | null;
  onCreate: (name: string) => Promise<CreatedKey | null>;
  onRevoke: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [revealed, setRevealed] = useState<CreatedKey | null>(null);
  const [copyFailed, setCopyFailed] = useState<string | null>(null);

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    setCopyFailed(null);
    const created = await onCreate(name);
    if (created) {
      setRevealed(created);
      setName("");
    }
  }

  function handleDismiss() {
    // The only place this token ever lived. Once this runs, no state
    // anywhere in the app still references it.
    setRevealed(null);
    setCopyFailed(null);
  }

  async function handleCopy(label: string, text: string) {
    const ok = await copyToClipboard(text);
    setCopyFailed(ok ? null : label);
  }

  function handleRevoke(key: ApiKeyRow) {
    if (!window.confirm(`Revoke the key "${key.name}"? Any client using it will fail its next call.`)) {
      return;
    }
    onRevoke(key.id);
  }

  const command = revealed ? claudeMcpAddCommand(revealed.name, endpointUrl, revealed.token) : "";
  const jsonConfig = revealed ? mcpJsonConfig(revealed.name, endpointUrl, revealed.token) : "";

  return (
    <Card>
      <CardHeader
        title="MCP access"
        description="Point an MCP client — Claude Code, Cursor, an internal agent — at this product using the same tools and the same tenant rules as the sales assistant itself."
      />
      <CardBody className="space-y-5">
        {agentDisabled ? (
          <Alert tone="warn">This agent is disabled, so its keys are refused.</Alert>
        ) : null}
        <div>
          <p className="text-sm font-medium text-ink">Endpoint</p>
          <p className="mt-1 break-all font-mono text-xs text-ink-muted">{endpointUrl}</p>
        </div>

        <div>
          <p className="text-sm font-medium text-ink">Exposed tools</p>
          {toolsFetching ? (
            <LoadingState label="Loading exposed tools…" />
          ) : exposedToolNames.length === 0 ? (
            <p className="mt-1 text-xs text-ink-muted">
              No tools are exposed yet — enable one on the Tools card above.
            </p>
          ) : (
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
              {exposedToolNames.map((toolName) => (
                <li
                  key={toolName}
                  className="rounded-control border border-line bg-surface-muted px-2 py-1 font-mono text-xs text-ink"
                >
                  {toolName}
                </li>
              ))}
            </ul>
          )}
        </div>

        {revealed ? (
          <div className="space-y-3 rounded-card border border-line-strong bg-surface-muted p-4">
            <Alert tone="warn">
              This token will not be shown again. Copy it, and the snippets below, now.
            </Alert>

            <div>
              <p className="text-sm font-medium text-ink">Token</p>
              <div className="mt-1 flex items-center gap-2">
                <code className="min-w-0 flex-1 break-all rounded-control border border-line bg-surface px-2 py-1.5 font-mono text-xs text-ink">
                  {revealed.token}
                </code>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => handleCopy("token", revealed.token)}
                >
                  Copy
                </Button>
              </div>
            </div>

            <div>
              <p className="text-sm font-medium text-ink">Claude Code</p>
              <div className="mt-1 flex items-start gap-2">
                <pre className="min-w-0 flex-1 overflow-x-auto rounded-control border border-line bg-surface px-2 py-1.5 font-mono text-xs text-ink">
                  {command}
                </pre>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => handleCopy("command", command)}
                >
                  Copy
                </Button>
              </div>
            </div>

            <div>
              <p className="text-sm font-medium text-ink">
                .mcp.json (Claude Code, Cursor and other HTTP-capable clients)
              </p>
              <div className="mt-1 flex items-start gap-2">
                <pre className="min-w-0 flex-1 overflow-x-auto rounded-control border border-line bg-surface px-2 py-1.5 font-mono text-xs text-ink">
                  {jsonConfig}
                </pre>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => handleCopy("config", jsonConfig)}
                >
                  Copy
                </Button>
              </div>
            </div>

            {copyFailed ? (
              <p role="status" className="text-xs text-ink-subtle">
                Could not copy the {copyFailed} automatically — select and copy it by hand.
              </p>
            ) : null}

            <Button type="button" variant="secondary" size="sm" onClick={handleDismiss}>
              Saved it — dismiss
            </Button>
          </div>
        ) : null}

        <div>
          <p className="mb-1.5 text-sm font-medium text-ink">API keys</p>
          {keysFetching ? (
            <LoadingState label="Loading keys…" />
          ) : keys.length === 0 ? (
            <p className="text-sm text-ink-muted">No API keys yet.</p>
          ) : (
            <ul className="divide-y divide-line">
              {keys.map((key) => (
                <li
                  key={key.id}
                  className="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0"
                >
                  <div className="min-w-0">
                    <p className="text-sm text-ink">{key.name}</p>
                    <p className="mt-0.5 font-mono text-xs text-ink-subtle">{key.keyPrefix}…</p>
                    <p className="mt-0.5 text-xs text-ink-subtle">
                      Created by {key.createdByName ?? "someone no longer on this team"} ·{" "}
                      {formatTimestamp(key.createdAt)} · Last used{" "}
                      {key.lastUsedAt ? formatTimestamp(key.lastUsedAt) : "Never"}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {key.revokedAt ? (
                      <Badge tone="neutral">Revoked</Badge>
                    ) : canManageKeys ? (
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleRevoke(key)}
                        loading={revokingId === key.id}
                        loadingLabel="Revoking…"
                      >
                        Revoke
                      </Button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {revokeError ? <Alert tone="danger">{revokeError}</Alert> : null}
      </CardBody>

      {canManageKeys ? (
        <form onSubmit={handleCreate}>
          <CardFooter>
            <div className="w-full space-y-3">
              {createError ? <Alert tone="danger">{createError}</Alert> : null}
              <div className="flex items-end gap-3">
                <div className="flex-1">
                  <Field label="New key name">
                    {(control) => (
                      <Input
                        {...control}
                        type="text"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="e.g. Zapier integration"
                        required
                      />
                    )}
                  </Field>
                </div>
                <Button type="submit" loading={creating} loadingLabel="Creating…">
                  Create key
                </Button>
              </div>
            </div>
          </CardFooter>
        </form>
      ) : null}
    </Card>
  );
}
