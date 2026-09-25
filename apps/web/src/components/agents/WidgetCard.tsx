"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { focusRing } from "@/components/ui/cn";
import { Input, Select, Textarea } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import type { AgentStatus, WidgetPosition } from "@/graphql/generated";
import { copyToClipboard } from "@/lib/mcp";
import { parseOriginLines, widgetSnippet } from "@/lib/widget-snippet";

export type WidgetSettingsData = {
  enabled: boolean;
  allowedOrigins: string[];
  brandColor: string;
  position: WidgetPosition;
  title: string | null;
  dailyMessageCap: number;
};

/** What `onSave` is called with -- the same shape `UpdateWidgetSettingsInput`
 * takes on the wire, already normalized client-side (trimmed domains, a
 * trimmed-to-`null` title). The server re-normalizes and re-validates the
 * origins regardless (`WidgetSettingsService.update`); this is only about not
 * sending a form's raw, unparsed textarea value. */
export type WidgetSettingsInput = {
  enabled: boolean;
  allowedOrigins: string[];
  brandColor: string;
  position: WidgetPosition;
  title: string | null;
  dailyMessageCap: number;
};

const DEFAULT_SETTINGS: WidgetSettingsData = {
  enabled: false,
  allowedOrigins: [],
  brandColor: "#2563eb",
  position: "RIGHT",
  title: null,
  dailyMessageCap: 500,
};

const POSITIONS: { value: WidgetPosition; label: string }[] = [
  { value: "RIGHT", label: "Right" },
  { value: "LEFT", label: "Left" },
];

/**
 * The agent page's website widget card (spec §7): enable the widget, set its
 * allowed domains, appearance and daily cap, copy the loader snippet, and see
 * a live preview of `/embed/{publicKey}` -- the same page a visitor's browser
 * would load.
 *
 * Self-contained rather than presentational like `McpAccessCard`: the page
 * hands it `agentId` and the loaded `settings` (plus the mutation's
 * fetching/error state), and this component owns the form fields, the
 * warnings and the preview. `onSave` is called with the normalized input and
 * returns whether it succeeded, so the card -- not the page -- decides to
 * bump the preview's `key` only on an actual save.
 */
export function WidgetCard({
  publicKey,
  agentStatus,
  leadToolEnabled,
  canEdit,
  settings,
  fetching = false,
  saving = false,
  saveError = null,
  onSave,
}: {
  /** Not read here -- the agent page uses it to build query variables and
   * the mutation, but the card's own markup only ever needs `publicKey`. It
   * stays in the prop type because it identifies *which* agent's settings
   * this card renders, which every caller should have to pass explicitly
   * rather than relying on `publicKey` alone happening to be unique. */
  agentId: string;
  publicKey: string;
  agentStatus: AgentStatus;
  leadToolEnabled: boolean;
  /** `false` for a member: every field renders disabled and there is no
   * Save button, the same read-only rule `McpAccessCard` applies to its
   * create form and Revoke buttons. */
  canEdit: boolean;
  settings?: WidgetSettingsData;
  fetching?: boolean;
  saving?: boolean;
  saveError?: string | null;
  onSave: (input: WidgetSettingsInput) => Promise<boolean>;
}) {
  const [enabled, setEnabled] = useState(DEFAULT_SETTINGS.enabled);
  const [originsText, setOriginsText] = useState("");
  const [brandColor, setBrandColor] = useState(DEFAULT_SETTINGS.brandColor);
  const [position, setPosition] = useState<WidgetPosition>(DEFAULT_SETTINGS.position);
  const [title, setTitle] = useState("");
  const [dailyMessageCap, setDailyMessageCap] = useState(DEFAULT_SETTINGS.dailyMessageCap);
  const [previewKey, setPreviewKey] = useState(0);
  const [copyFailed, setCopyFailed] = useState(false);

  // Seeded from the loaded (or just-saved-and-refetched) settings, never as
  // initial state: the query has not resolved on the very first render, and
  // re-running this on every `settings` identity change is what makes a save
  // followed by a refetch show the server's own normalized values (deduped,
  // lower-cased) rather than what the form last had queued.
  useEffect(() => {
    const source = settings ?? DEFAULT_SETTINGS;
    setEnabled(source.enabled);
    setOriginsText(source.allowedOrigins.join("\n"));
    setBrandColor(source.brandColor);
    setPosition(source.position);
    setTitle(source.title ?? "");
    setDailyMessageCap(source.dailyMessageCap);
  }, [settings]);

  const parsedOrigins = parseOriginLines(originsText);
  const snippet = widgetSnippet(
    typeof window !== "undefined" ? window.location.origin : "",
    publicKey,
  );

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const input: WidgetSettingsInput = {
      enabled,
      allowedOrigins: parsedOrigins,
      brandColor,
      position,
      title: title.trim() || null,
      dailyMessageCap,
    };
    const ok = await onSave(input);
    if (ok) {
      setPreviewKey((key) => key + 1);
    }
  }

  async function handleCopy() {
    const ok = await copyToClipboard(snippet);
    setCopyFailed(!ok);
  }

  if (fetching && !settings) {
    return (
      <Card>
        <CardHeader title="Website widget" description="Let visitors chat with this agent from your site." />
        <CardBody>
          <LoadingState label="Loading widget settings…" />
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Website widget"
        description="Paste one script tag into your site and visitors can chat with this agent."
      />
      <form onSubmit={handleSubmit}>
        <CardBody className="space-y-5">
          {agentStatus !== "ACTIVE" ? (
            <Alert tone="warn">Visitors will see nothing until the agent is active.</Alert>
          ) : null}
          {enabled && parsedOrigins.length === 0 ? (
            <Alert tone="warn">With no allowed domains set, only the preview will load.</Alert>
          ) : null}
          {leadToolEnabled ? (
            <Alert tone="warn">
              The create_lead tool is granted for this agent — anonymous visitors can submit leads
              through the widget.
            </Alert>
          ) : null}
          {saveError ? <Alert tone="danger">{saveError}</Alert> : null}

          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={enabled}
              disabled={!canEdit}
              onChange={(e) => setEnabled(e.target.checked)}
              className={`accent-primary ${focusRing} focus-visible:ring-offset-1`}
            />
            Enable the widget
          </label>

          <Field
            label="Allowed domains"
            description="One origin per line (https://example.com) — the sites this widget is allowed to appear on."
          >
            {(control) => (
              <Textarea
                {...control}
                value={originsText}
                disabled={!canEdit}
                onChange={(e) => setOriginsText(e.target.value)}
                rows={4}
                placeholder="https://example.com"
              />
            )}
          </Field>
          <p className="text-xs text-ink-subtle">
            This list only stops other websites from framing the widget in a browser — it does not
            stop direct use of the chat API with the public key. Rate limits and the daily cap below
            are the real guard against abuse.
          </p>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Brand colour" description="Used for the launcher and the header.">
              {(control) => (
                <div className="flex items-center gap-2">
                  <input
                    type="color"
                    aria-label="Colour swatch"
                    value={/^#[0-9a-fA-F]{6}$/.test(brandColor) ? brandColor : "#2563eb"}
                    disabled={!canEdit}
                    onChange={(e) => setBrandColor(e.target.value)}
                    className="h-9 w-9 shrink-0 cursor-pointer rounded-control border border-line-strong disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <Input
                    {...control}
                    value={brandColor}
                    disabled={!canEdit}
                    onChange={(e) => setBrandColor(e.target.value)}
                    placeholder="#2563eb"
                  />
                </div>
              )}
            </Field>

            <Field label="Position">
              {(control) => (
                <Select
                  {...control}
                  value={position}
                  disabled={!canEdit}
                  onChange={(e) => setPosition(e.target.value as WidgetPosition)}
                >
                  {POSITIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>

          <Field label="Title" description="Falls back to the agent's own name when left blank.">
            {(control) => (
              <Input
                {...control}
                type="text"
                value={title}
                disabled={!canEdit}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={60}
              />
            )}
          </Field>

          <Field
            label="Daily message cap"
            description="Visitor messages per day, across every visitor, before the widget stops answering."
          >
            {(control) => (
              <Input
                {...control}
                type="number"
                min={1}
                max={100000}
                value={dailyMessageCap}
                disabled={!canEdit}
                onChange={(e) => setDailyMessageCap(Number(e.target.value))}
              />
            )}
          </Field>

          <div>
            <p className="text-sm font-medium text-ink">Loader snippet</p>
            <div className="mt-1 flex items-start gap-2">
              <pre className="min-w-0 flex-1 overflow-x-auto rounded-control border border-line bg-surface-muted px-2 py-1.5 font-mono text-xs text-ink">
                {snippet}
              </pre>
              <Button type="button" variant="secondary" size="sm" onClick={handleCopy}>
                Copy
              </Button>
            </div>
            {copyFailed ? (
              <p role="status" className="mt-1 text-xs text-ink-subtle">
                Could not copy automatically — select and copy the snippet by hand.
              </p>
            ) : null}
          </div>

          <div>
            <p className="text-sm font-medium text-ink">Preview</p>
            {settings?.enabled ? (
              <iframe
                key={previewKey}
                title="Widget preview"
                src={`/embed/${encodeURIComponent(publicKey)}`}
                width={360}
                height={560}
                className="mt-1 rounded-card border border-line"
              />
            ) : (
              <p className="mt-1 rounded-card border border-line bg-surface-muted px-3 py-8 text-center text-sm text-ink-muted">
                The preview appears once the widget is enabled and saved.
              </p>
            )}
          </div>
        </CardBody>

        {canEdit ? (
          <CardFooter>
            <Button type="submit" loading={saving} loadingLabel="Saving…">
              Save widget settings
            </Button>
          </CardFooter>
        ) : null}
      </form>
    </Card>
  );
}
