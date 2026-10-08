import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input, Select } from "@/components/ui/Input";
import type { ConfirmSupplierMappingInput, SupplierQuoteField } from "@/graphql/generated";
import { formatTimestamp } from "@/lib/format";
import {
  buildMappingInput,
  emptyMappingDraft,
  MAPPING_FIELDS,
  type MappingDraft,
  mappingProblems,
  messageErrorLabel,
  messageStatusLabel,
} from "@/lib/supplier-quotes";

export type MappingRow = {
  id: string;
  supplierId: string;
  worksheetName: string;
  headerRow: number;
  columns: { field: SupplierQuoteField; label: string }[];
  columnLabels: string[];
  defaultCurrency: string | null;
  defaultPurchaseUnit: string | null;
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
 * not be processed. A proposal carries the worksheet's header *labels* and
 * nothing else: no sample cell values are stored, so none can be shown.
 */
export function MappingReview({
  mappings,
  messages,
  supplierNames,
  confirmingId,
  errors,
  onConfirm,
}: {
  mappings: MappingRow[];
  messages: ReviewMessageRow[];
  supplierNames: Record<string, string>;
  confirmingId: string | null;
  errors: Record<string, string>;
  onConfirm: (mapping: MappingRow, input: ConfirmSupplierMappingInput) => void;
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
  onConfirm,
}: {
  mapping: MappingRow;
  supplierName: string;
  confirming: boolean;
  error: string | null;
  onConfirm: (mapping: MappingRow, input: ConfirmSupplierMappingInput) => void;
}) {
  // Seeded once from the proposal; a refetch that brings a new version of
  // this proposal arrives under a new id and so a new form.
  const [draft, setDraft] = useState<MappingDraft>(() => emptyMappingDraft(mapping));
  const problems = mappingProblems(draft, mapping.columnLabels);

  function setField(field: SupplierQuoteField, label: string) {
    setDraft((current) => ({ ...current, fields: { ...current.fields, [field]: label } }));
  }

  return (
    <form
      aria-label={`Spreadsheet layout from ${supplierName}`}
      className="space-y-4 px-5 py-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (problems.length === 0) onConfirm(mapping, buildMappingInput(mapping, draft));
      }}
    >
      <div>
        <h3 className="text-sm font-semibold text-ink">{supplierName}</h3>
        <p className="mt-1 text-xs text-ink-muted">
          New spreadsheet layout: worksheet “{mapping.worksheetName}”, header row {mapping.headerRow}. Check which
          column holds each value, then confirm to analyse the email with it.
        </p>
        <p className="mt-2 text-xs text-ink-subtle">Columns found</p>
        <p className="text-sm text-ink">{mapping.columnLabels.join(", ")}</p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {MAPPING_FIELDS.map(({ field, label }) => (
          <Field key={field} label={label}>
            {(control) => (
              <Select {...control} value={draft.fields[field]} onChange={(event) => setField(field, event.target.value)}>
                <option value="">Not in this sheet</option>
                {mapping.columnLabels.map((column) => (
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
              maxLength={16}
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
