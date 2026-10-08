import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input, Select } from "@/components/ui/Input";
import { formatTimestamp } from "@/lib/format";
import {
  buildMappingBody,
  chooseSheet,
  type ConfirmMappingBody,
  draftLabels,
  emptyMappingDraft,
  MAPPING_FIELDS,
  type MappingDraft,
  type MappingProposal,
  mappingProblems,
  messageErrorLabel,
  messageStatusLabel,
  sheetChoices,
  sheetKey,
} from "@/lib/supplier-quotes";

export type MappingRow = MappingProposal & {
  id: string;
  supplierId: string;
};

export type ReviewMessageRow = {
  id: string;
  senderEmail: string;
  receivedAt: string | null;
  status: string;
  errorCode: string | null;
  attachmentCount: number;
};

/**
 * Proposed workbook layouts waiting for an owner, plus the emails that could
 * not be processed. A proposal carries header *labels* only -- of its own
 * row and of every other header-like row in the file, so a proposal on the
 * wrong sheet can be moved -- and never cell values, so none are shown.
 */
export function MappingReview({
  mappings,
  messages,
  supplierNames,
  confirmingId,
  errors,
  canManage,
  onConfirm,
}: {
  mappings: MappingRow[];
  messages: ReviewMessageRow[];
  supplierNames: Record<string, string>;
  confirmingId: string | null;
  errors: Record<string, string>;
  /** Only the owner configures how supplier files are read. */
  canManage: boolean;
  onConfirm: (mapping: MappingRow, body: ConfirmMappingBody) => void;
}) {
  if (mappings.length === 0 && messages.length === 0) {
    return (
      <EmptyState
        icon="check"
        title="Nothing to review"
        description="New spreadsheet layouts and emails that could not be read will wait here for you."
      />
    );
  }
  return (
    <div className="divide-y divide-line">
      {mappings.map((mapping) => (
        <MappingForm
          key={mapping.id}
          mapping={mapping}
          supplierName={supplierNames[mapping.supplierId] ?? "Unknown supplier"}
          confirming={confirmingId === mapping.id}
          error={errors[mapping.id] ?? null}
          canManage={canManage}
          onConfirm={onConfirm}
        />
      ))}
      {messages.length > 0 ? (
        <div className="px-5 py-4">
          <h3 className="text-sm font-semibold text-ink">Emails that need attention</h3>
          <ul className="mt-2 divide-y divide-line">
            {messages.map((message) => {
              const status = messageStatusLabel(message.status);
              return (
                <li key={message.id} className="flex flex-wrap items-center gap-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-ink">{message.senderEmail}</p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      {message.receivedAt ? formatTimestamp(message.receivedAt) : "Date unknown"}
                      {" · "}
                      {message.attachmentCount === 1 ? "1 spreadsheet" : `${message.attachmentCount} spreadsheets`}
                    </p>
                    {message.errorCode ? (
                      <p className="mt-0.5 text-xs text-ink-muted">{messageErrorLabel(message.errorCode)}</p>
                    ) : null}
                  </div>
                  <Badge tone={status.tone}>{status.label}</Badge>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function MappingForm({
  mapping,
  supplierName,
  confirming,
  error,
  canManage,
  onConfirm,
}: {
  mapping: MappingRow;
  supplierName: string;
  confirming: boolean;
  error: string | null;
  canManage: boolean;
  onConfirm: (mapping: MappingRow, body: ConfirmMappingBody) => void;
}) {
  // Seeded once from the proposal; a refetch that brings a new version of
  // this proposal arrives under a new id and so a new form.
  const [draft, setDraft] = useState<MappingDraft>(() => emptyMappingDraft(mapping));
  const choices = sheetChoices(mapping);
  const chosen = choices.find((option) => sheetKey(option) === draft.sheet) ?? choices[0];
  const labels = draftLabels(draft, mapping);
  const problems = mappingProblems(draft, labels);

  function setField(field: (typeof MAPPING_FIELDS)[number]["field"], label: string) {
    setDraft((current) => ({ ...current, fields: { ...current.fields, [field]: label } }));
  }

  const intro = (
    <div>
      <h3 className="text-sm font-semibold text-ink">{supplierName}</h3>
      <p className="mt-1 text-xs text-ink-muted">
        New spreadsheet layout: worksheet “{chosen.worksheetName}”, header row {chosen.headerRow}.
        {canManage
          ? " Check which column holds each value, then confirm to analyse the email with it."
          : null}
      </p>
      <p className="mt-2 text-xs text-ink-subtle">Columns found</p>
      <p className="text-sm text-ink">{labels.join(", ")}</p>
    </div>
  );

  if (!canManage) {
    return (
      <div className="space-y-2 px-5 py-4">
        {intro}
        <p className="text-xs text-ink-subtle">Only the organization owner can confirm a spreadsheet layout.</p>
      </div>
    );
  }

  return (
    <form
      aria-label={`Spreadsheet layout from ${supplierName}`}
      className="space-y-4 px-5 py-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (problems.length === 0) onConfirm(mapping, buildMappingBody(draft, mapping));
      }}
    >
      {intro}

      {choices.length > 1 ? (
        <Field
          label="Worksheet and header row"
          description="Pick another if the proposal landed on a cover page or a title row."
        >
          {(control) => (
            <Select
              {...control}
              value={draft.sheet}
              onChange={(event) => setDraft((current) => chooseSheet(current, mapping, event.target.value))}
            >
              {choices.map((option) => (
                <option key={sheetKey(option)} value={sheetKey(option)} title={option.labels.join(", ")}>
                  {option.worksheetName} — row {option.headerRow}
                </option>
              ))}
            </Select>
          )}
        </Field>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {MAPPING_FIELDS.map(({ field, label }) => (
          <Field key={field} label={label}>
            {(control) => (
              <Select {...control} value={draft.fields[field]} onChange={(event) => setField(field, event.target.value)}>
                <option value="">Not in this sheet</option>
                {labels.map((column) => (
                  <option key={column} value={column} title={column}>
                    {column}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        ))}
        <Field label="Default currency" description="Used when no column holds the currency.">
          {(control) => (
            <Input
              {...control}
              value={draft.defaultCurrency}
              maxLength={3}
              placeholder="e.g. USD"
              onChange={(event) => setDraft({ ...draft, defaultCurrency: event.target.value })}
            />
          )}
        </Field>
        <Field label="Default purchase unit" description="Used when no column holds the unit.">
          {(control) => (
            <Input
              {...control}
              value={draft.defaultPurchaseUnit}
              maxLength={64}
              placeholder="e.g. box"
              onChange={(event) => setDraft({ ...draft, defaultPurchaseUnit: event.target.value })}
            />
          )}
        </Field>
      </div>

      {problems.length > 0 ? (
        <Alert tone="warn">
          <ul className="list-disc space-y-0.5 pl-4">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </Alert>
      ) : null}
      {error ? <Alert tone="danger">{error}</Alert> : null}

      <Button type="submit" disabled={problems.length > 0} loading={confirming} loadingLabel="Confirming…">
        Confirm mapping
      </Button>
    </form>
  );
}
